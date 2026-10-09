"""MCP Server（对 Agent 暴露）。

阶段 2 范围：**只读**工具。所有工具都直接调用 ``PluginService``，
与网页控制台共用同一实现（禁止两套逻辑）。

安全立场：
- Agent **不能**通过 MCP 执行安装 / 回滚 / 任何写操作；
  它只能"提交操作申请"，授权必须来自用户在受信任网页上的明确确认（阶段 5）。
- 工具返回值中**不得**包含令牌、绝对路径中的敏感部分等。
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from app import __version__
from app.models import PluginKind
from app.runtime import Runtime
from app.security import sanitize_for_log
from app.services import PluginNotFound, dump_plugin

# 单次返回上限，避免 Agent 侧上下文被撑爆
_MAX_LIMIT = 50


def build_server(runtime: Runtime) -> MCPServer:
    """构造 MCPServer 实例（``mcp 2.x``：``mcp.server.mcpserver.MCPServer``）。"""
    service = runtime.plugins

    server = MCPServer(
        name="mcp-manager",
        version=__version__,
        instructions=(
            "本地优先的 MCP / Agent 插件管理器。"
            "本服务器当前仅提供只读查询能力；安装与回滚必须由用户在网页控制台确认。"
        ),
    )

    @server.tool(description="列出已登记的插件条目（只读）。")
    def list_plugins(kind: str | None = None, limit: int = 20) -> dict[str, Any]:
        parsed_kind = None
        if kind:
            try:
                parsed_kind = PluginKind(kind)
            except ValueError:
                return {
                    "ok": False,
                    "error": "unknown_kind",
                    "detail": f"kind 必须是 {[k.value for k in PluginKind]} 之一",
                }
        limit = max(1, min(int(limit), _MAX_LIMIT))
        items = service.list(kind=parsed_kind, limit=limit)
        return {
            "ok": True,
            "count": len(items),
            "items": [
                {
                    "id": p.id,
                    "name": p.name,
                    "kind": p.kind.value,
                    "risk_level": p.risk_level.value,
                    "review_status": p.review_status.value,
                    "install_status": p.install_status.value,
                    "score_total": p.score.total(),
                }
                for p in items
            ],
        }

    @server.tool(description="按 ID 查询单个插件条目的完整信息（只读）。")
    def get_plugin(plugin_id: str) -> dict[str, Any]:
        try:
            plugin = service.get(plugin_id)
        except PluginNotFound:
            return {"ok": False, "error": "not_found", "detail": plugin_id}
        return {"ok": True, "plugin": dump_plugin(plugin)}

    @server.tool(description="获取插件库统计信息（只读）。")
    def get_stats() -> dict[str, Any]:
        return {"ok": True, "stats": service.stats()}

    @server.tool(
        description=(
            "提交一个安装申请：生成**待用户确认**的安装计划（不会立即安装）。"
            "Agent 只能走到这一步；确认与执行必须由用户在受信任网页上完成。"
        )
    )
    def request_install(plugin_id: str, reason: str) -> dict[str, Any]:
        if runtime.installs is None:  # pragma: no cover - 装配缺失
            return {"ok": False, "error": "install_unavailable"}
        try:
            plugin = service.get(plugin_id)
        except PluginNotFound:
            service.audit(
                actor="agent",
                action="plugin.request_install",
                target=plugin_id,
                outcome="denied",
                detail="插件条目不存在",
            )
            return {"ok": False, "error": "not_found", "detail": plugin_id}
        try:
            op = runtime.installs.create_plan(plugin, action="install", actor="agent")
        except Exception as exc:  # noqa: BLE001 - 如实回报失败原因，绝不伪成功
            service.audit(
                actor="agent",
                action="plugin.request_install",
                target=plugin_id,
                outcome="denied",
                detail=sanitize_for_log(exc),
            )
            return {
                "ok": False,
                "error": "plan_rejected",
                "detail": sanitize_for_log(exc),
            }
        service.audit(
            actor="agent",
            action="plugin.request_install",
            target=plugin_id,
            outcome="ok",
            detail=f"已生成待确认计划 operation_id={op.id}",
        )
        return {
            "ok": True,
            "operation_id": op.id,
            "status": op.status,
            "detail": (
                "安装计划已生成，等待用户在网页控制台确认；"
                "Agent 无法自行确认或执行。"
            ),
        }

    return server


def main() -> None:  # pragma: no cover - 入口，手工运行
    """以 stdio 传输启动（默认，供 MCP 客户端拉起）。"""
    runtime = Runtime.create()
    build_server(runtime).run("stdio")


if __name__ == "__main__":  # pragma: no cover
    main()
"""安装编排服务（阶段 5）。

把 10 步安装流程中涉及**授权**的第 1–3 步与文件系统侧的第 4–10 步串起来：

    生成计划 → 用户通过受信任网页确认 → 校验令牌与计划一致 → 执行 → 记录归属

纪律（不可违反）：

- ``create_plan`` **不产生任何写入**；
- ``execute`` 必须携带一次性确认令牌，令牌绑定计划摘要；计划变化即失效；
- 失败绝不伪成功：任何异常都会把操作置为 ``failed`` 并记录真实原因。
"""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from app.install.confirmation import (
    confirmation_status,
    create_confirmation,
    verify_confirmation,
)
from app.install.fsguard import owned_paths
from app.install.installer import ApplyResult, InstallError, Installer
from app.install.operation import (
    Operation,
    OperationError,
    OperationStatus,
    OperationStore,
)
from app.install.plan import (
    InstallPlan,
    PlannedFile,
    build_binding,
    default_expiry,
)
from app.models import Plugin

# 计划 / 确认令牌默认有效期（秒）
PLAN_TTL_SECONDS = 1800


class InstallUnavailable(RuntimeError):
    """安装能力不可用（配置禁用 / 服务未装配）。"""


class InstallServiceError(RuntimeError):
    """编排层状态错误（状态不允许 / 令牌不匹配 / 前置条件不满足）。"""


class InstallService:
    """安装 / 回滚 / 卸载的统一编排入口（网页与 MCP 共用同一实现）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        installer: Installer,
        *,
        plugin_lookup: Callable[[str], Plugin] | None = None,
        confirmation_ttl: int = PLAN_TTL_SECONDS,
    ) -> None:
        self._conn = conn
        self._installer = installer
        self._lookup = plugin_lookup
        self._ops = OperationStore(conn)
        self._ttl = max(60, int(confirmation_ttl))

    # ------------------------------------------------------------------ #
    # 第 1 步：生成计划（无副作用）
    # ------------------------------------------------------------------ #
    def create_plan(
        self, plugin: Plugin, *, action: str = "install", actor: str = "user"
    ) -> Operation:
        plan = self._build_plan(plugin, action=action)
        return self._ops.create(
            plugin_id=plugin.id,
            action=action,
            actor=actor,
            status=OperationStatus.AWAITING_CONFIRMATION,
            plan=plan,
            plan_digest=plan.plan_digest,
        )

    def _build_plan(self, plugin: Plugin, *, action: str) -> InstallPlan:
        if action == "install":
            return self._installer.build_plan(plugin)
        if action in ("uninstall", "rollback"):
            return self._removal_plan(plugin, action=action)
        raise InstallServiceError(f"未知操作类型：{action!r}")

    def _removal_plan(self, plugin: Plugin, *, action: str) -> InstallPlan:
        """卸载 / 回滚计划：只列出**本系统登记并拥有**的文件。"""
        paths = owned_paths(self._conn, plugin.id)
        if not paths:
            raise InstallServiceError(
                "该插件没有任何本系统登记并拥有的文件，拒绝生成"
                f"{'卸载' if action == 'uninstall' else '回滚'}计划。"
            )
        target_dir = self._installer.roots.plugin_dir(plugin.id)
        files = [
            PlannedFile(path=str(path), action="delete", size=0) for path in paths
        ]
        rollback_of = ""
        if action == "rollback":
            previous = self._ops.latest_succeeded(plugin.id, action="install")
            if previous is None:
                raise InstallServiceError("没有可回滚的成功安装记录")
            rollback_of = previous.id

        return InstallPlan(
            plugin_id=plugin.id,
            plugin_name=plugin.name,
            kind=plugin.kind.value,
            source=plugin.source,
            pinned_ref=plugin.pinned_ref or "",
            target_dir=str(target_dir),
            config_path=None,
            files=files,
            commands=[],
            permissions=["filesystem:write:managed"],
            runtime_requirements=["无需 root；仅操作受管目录"],
            network_required=False,
            spawns_process=False,
            review={
                "reviewed": False,
                "coverage": "none",
                "note": "卸载 / 回滚不重新拉取来源，仅处理本系统拥有的变更。",
                "disclaimer": (
                    "本计划仅列出本系统归属台账中登记的文件；"
                    "台账之外的文件不会被触碰。"
                ),
            },
            rollback_plan=(
                "回滚将按安装前快照还原；"
                if action == "rollback"
                else "卸载不可逆；如需恢复请重新安装。"
            ),
            failure_handling="任一步失败即中止，操作标记为 failed，绝不视为成功。",
            notes=(
                [f"将删除 {len(files)} 个受管文件。"]
                + ([f"rollback_of={rollback_of}"] if rollback_of else [])
            ),
            expires_at=default_expiry(self._ttl),
        ).sealed()

    # ------------------------------------------------------------------ #
    # 第 2 步：用户确认（由受信任网页触发）
    # ------------------------------------------------------------------ #
    def confirm(self, operation_id: str, *, actor: str = "user") -> str:
        """生成一次性确认令牌（明文只返回一次）。"""
        op = self._ops.get(operation_id)
        if op.status != OperationStatus.AWAITING_CONFIRMATION.value:
            raise InstallServiceError(f"当前状态不允许确认：{op.status}")
        plan = op.plan
        if plan is None:
            raise InstallServiceError("操作缺少安装计划")
        if plan.is_expired():
            raise InstallServiceError("安装计划已过期，请重新生成计划")

        binding = build_binding(op.id, plan)
        token, _ = create_confirmation(
            self._conn,
            operation_id=op.id,
            binding=binding,
            ttl_seconds=self._ttl,
        )
        self._ops.transition(
            op.id,
            OperationStatus.APPROVED,
            allowed_from=(OperationStatus.AWAITING_CONFIRMATION,),
            step="confirm",
            message="用户已通过受信任网页确认",
        )
        return token

    def cancel(self, operation_id: str, *, actor: str = "user") -> Operation:
        return self._ops.transition(
            operation_id,
            OperationStatus.CANCELLED,
            allowed_from=(
                OperationStatus.AWAITING_CONFIRMATION,
                OperationStatus.APPROVED,
            ),
            step="cancel",
            message="操作已取消",
        )

    # ------------------------------------------------------------------ #
    # 第 3–10 步：执行
    # ------------------------------------------------------------------ #
    def execute(self, operation_id: str, *, token: str, actor: str = "user") -> Operation:
        op = self._ops.get(operation_id)
        if op.status != OperationStatus.APPROVED.value:
            raise InstallServiceError(
                f"操作尚未获得有效确认（当前 {op.status}），拒绝执行"
            )
        plan = op.plan
        if plan is None:
            raise InstallServiceError("操作缺少安装计划")
        if plan.is_expired():
            raise InstallServiceError("安装计划已过期，必须重新生成并确认")

        # 执行前**重新验证**：令牌一次性、未过期、绑定摘要与当前计划一致
        verify_confirmation(
            self._conn,
            operation_id=op.id,
            token=token,
            current_binding=build_binding(op.id, plan),
            consume=True,
        )
        self._ops.transition(
            op.id,
            OperationStatus.RUNNING,
            allowed_from=(OperationStatus.APPROVED,),
            step="execute",
            message="开始执行",
        )
        try:
            result = self._dispatch(op, plan, actor=actor)
        except Exception as exc:  # noqa: BLE001 - 必须记录真实失败原因
            self._ops.transition(
                op.id,
                OperationStatus.FAILED,
                allowed_from=(OperationStatus.RUNNING,),
                error=str(exc),
                step="failed",
                level="error",
                message=f"执行失败：{exc}",
            )
            raise
        return self._ops.transition(
            op.id,
            OperationStatus.SUCCEEDED,
            allowed_from=(OperationStatus.RUNNING,),
            result=result,
            step="done",
            message="执行完成",
        )

    def _dispatch(
        self, op: Operation, plan: InstallPlan, *, actor: str
    ) -> dict[str, Any]:
        if op.action == "install":
            plugin = self._require_plugin(op.plugin_id)
            applied: ApplyResult = self._installer.apply(
                plugin, plan, operation_id=op.id
            )
            return {"applied": applied.to_dict()}
        if op.action == "uninstall":
            removed = self._installer.uninstall(plugin_id=op.plugin_id)
            return {"removed": removed}
        if op.action == "rollback":
            target = self._rollback_target(plan)
            restored: list[str] = []
            if target:
                restored = self._installer.revert(operation_id=target)
            removed = self._installer.uninstall(plugin_id=op.plugin_id)
            return {
                "restored": restored,
                "removed": removed,
                "rollback_of": target or None,
            }
        raise InstallServiceError(f"未知操作类型：{op.action!r}")

    @staticmethod
    def _rollback_target(plan: InstallPlan) -> str | None:
        for note in plan.notes:
            if note.startswith("rollback_of="):
                value = note.split("=", 1)[1].strip()
                return value or None
        return None

    def _require_plugin(self, plugin_id: str) -> Plugin:
        if self._lookup is None:
            raise InstallUnavailable("未配置插件查询函数，无法读取插件条目")
        return self._lookup(plugin_id)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def get(self, operation_id: str) -> Operation:
        return self._ops.get(operation_id)

    def list(
        self,
        *,
        plugin_id: str | None = None,
        status: OperationStatus | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        return self._ops.list(plugin_id=plugin_id, status=status, limit=limit)

    def logs(self, operation_id: str, *, limit: int = 200):
        return self._ops.logs(operation_id, limit=limit)

    def confirmation(self, operation_id: str) -> dict[str, object] | None:
        return confirmation_status(self._conn, operation_id)

    def recover_interrupted(self) -> list[str]:
        """进程启动时调用：把遗留的 running / rolling_back 事务标记为 interrupted。"""
        return self._ops.mark_interrupted()

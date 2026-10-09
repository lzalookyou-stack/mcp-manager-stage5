"""FastAPI 应用装配。

阶段 2 范围：**只读** API + 静态控制台页面 + 基础安全中间件。
阶段 4 范围：在**保持无写接口**的前提下，增加控制台交互所需的只读端点
（远程搜索 / 深度检查 / 安全审查 / 候选对比 / SSE 事件流）。

- 仍然**不提供任何写接口**（安装/卸载等写操作在阶段 5 引入，届时必须带 CSRF 与会话）；
- 所有请求先过 Host / Origin / 体积检查。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.install import InstallError
from app.install.confirmation import ConfirmationError
from app.install.operation import OperationError
from app.install.service import (
    InstallService,
    InstallServiceError,
    InstallUnavailable,
)
from app.runtime import Runtime
from app.search.github_client import GitHubError
from app.security import SecurityError, sanitize_for_log
from app.services import (
    PluginNotFound,
    SearchUnavailable,
    ValidationError,
    dump_plugin,
)
from app.session import CSRF_HEADER, SESSION_COOKIE, SessionError

# 不需要 Origin 校验的"安全方法"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# SSE 心跳间隔（秒）：无事件时定期发注释行，避免中间层判死连接。
_SSE_HEARTBEAT_SECONDS = 15.0
# 单个插件单次审查最多抓取的文件数上限（防止前端传入过大值）
_MAX_REVIEW_FILES_CAP = 40

# 统一安全响应头。前端不使用内联脚本 / 内联样式，因此 CSP 可以收紧到 'self'。
_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}


def _json_response(status: int, payload: dict[str, Any]) -> JSONResponse:
    """构造带统一安全头的 JSON 响应。"""
    resp = JSONResponse(status_code=status, content=payload)
    for key, value in _SECURITY_HEADERS.items():
        resp.headers[key] = value
    return resp


def create_app(runtime: Runtime | None = None) -> FastAPI:
    rt = runtime or Runtime.create()

    app = FastAPI(
        title="mcp-manager",
        version=__version__,
        docs_url=None,       # 关闭交互式文档，减少攻击面
        redoc_url=None,
        openapi_url=None,
    )
    app.state.runtime = rt

    # ------------------------------------------------------------------ #
    # 安全中间件
    # ------------------------------------------------------------------ #
    @app.middleware("http")
    async def _security_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        settings = rt.settings

        # 1) Host 头校验：防止 DNS rebinding 指向本机服务
        host_header = request.headers.get("host", "")
        hostname = host_header.rsplit(":", 1)[0].strip("[]").lower()
        if hostname and hostname not in {
            h.lower() for h in settings.allowed_hosts
        }:
            return _json_response(
                400, {"error": "invalid_host", "detail": "Host 头不在允许列表内"}
            )

        # 2) 体积限制
        raw_len = request.headers.get("content-length")
        if raw_len is not None:
            try:
                if int(raw_len) > settings.max_request_bytes:
                    return _json_response(413, {"error": "payload_too_large"})
            except ValueError:
                return _json_response(400, {"error": "invalid_content_length"})

        # 3) 非安全方法必须携带允许的 Origin（CSRF 基线）
        if request.method not in _SAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is None or origin not in settings.allowed_origins:
                return _json_response(403, {"error": "csrf_origin_rejected"})

        response = await call_next(request)
        for key, value in _SECURITY_HEADERS.items():
            response.headers[key] = value
        return response

    # ------------------------------------------------------------------ #
    # 异常映射（不泄露内部细节）
    # ------------------------------------------------------------------ #
    @app.exception_handler(PluginNotFound)
    async def _not_found(_: Request, exc: PluginNotFound) -> JSONResponse:
        return _json_response(
            404, {"error": "plugin_not_found", "detail": str(exc)}
        )

    @app.exception_handler(ValidationError)
    @app.exception_handler(SecurityError)
    async def _bad_request(_: Request, exc: Exception) -> JSONResponse:
        return _json_response(
            400, {"error": "invalid_request", "detail": sanitize_for_log(exc)}
        )

    @app.exception_handler(SearchUnavailable)
    async def _search_unavailable(_: Request, exc: SearchUnavailable) -> JSONResponse:
        # 显式失败：绝不退化为"返回空结果"（空结果会被误读为"没有找到"）。
        return _json_response(
            503, {"error": "search_unavailable", "detail": sanitize_for_log(exc)}
        )

    @app.exception_handler(GitHubError)
    async def _github_error(_: Request, exc: GitHubError) -> JSONResponse:
        # 502：上游（GitHub）失败，如实透出 kind，便于前端区分限流/网络/权限。
        return _json_response(
            502,
            {
                "error": "github_error",
                "kind": getattr(exc, "kind", "error"),
                "detail": sanitize_for_log(exc),
            },
        )

    @app.exception_handler(SessionError)
    async def _session_rejected(_: Request, exc: SessionError) -> JSONResponse:
        # 会话 / CSRF 校验失败：一律拒绝，绝不降级放行。
        return _json_response(
            403, {"error": "session_rejected", "detail": sanitize_for_log(exc)}
        )

    @app.exception_handler(InstallUnavailable)
    async def _install_unavailable(_: Request, exc: InstallUnavailable) -> JSONResponse:
        # 能力不可用（配置禁用 / 未装配）：403，绝不降级放行。
        return _json_response(
            403, {"error": "install_unavailable", "detail": sanitize_for_log(exc)}
        )

    @app.exception_handler(OperationError)
    @app.exception_handler(ConfirmationError)
    @app.exception_handler(InstallServiceError)
    async def _operation_conflict(_: Request, exc: Exception) -> JSONResponse:
        # 状态不允许 / 令牌不匹配 / 计划已变化：409，提示必须重新走确认流程。
        return _json_response(
            409, {"error": "operation_rejected", "detail": sanitize_for_log(exc)}
        )

    @app.exception_handler(InstallError)
    async def _install_failed(_: Request, exc: InstallError) -> JSONResponse:
        # 执行失败：操作状态已如实记为 failed，这里只回传失败原因。
        return _json_response(
            500, {"error": "install_failed", "detail": sanitize_for_log(exc)}
        )

    # ------------------------------------------------------------------ #
    # API
    # ------------------------------------------------------------------ #
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "loopback_only": rt.settings.is_loopback,
            "plugins": rt.plugins.count(),
        }

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return rt.plugins.stats()

    @app.get("/api/plugins")
    async def list_plugins(
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        from app.models import PluginKind

        parsed_kind = None
        if kind:
            try:
                parsed_kind = PluginKind(kind)
            except ValueError:
                raise HTTPException(status_code=400, detail=f"未知 kind：{kind}")
        items = rt.plugins.list(kind=parsed_kind, limit=limit, offset=offset)
        return {"count": len(items), "items": [dump_plugin(p) for p in items]}

    @app.get("/api/plugins/{plugin_id}")
    async def get_plugin(plugin_id: str) -> dict[str, Any]:
        return dump_plugin(rt.plugins.get(plugin_id))

    @app.get("/api/audit")
    async def audit(limit: int = 50) -> dict[str, Any]:
        rows = rt.plugins.list_audit(limit=limit)
        return {"count": len(rows), "items": [r.__dict__ for r in rows]}

    # ------------------------------------------------------------------ #
    # 阶段 4：发现（只读 GET；不提供任何写接口）
    # ------------------------------------------------------------------ #
    @app.get("/api/query")
    async def preview_query(
        q: str,
        language: str | None = None,
        min_stars: int | None = None,
        pushed_after: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        """把自然语言需求转成 GitHub 检索式（**不发起网络请求**，便于前端预览）。"""
        filters: dict[str, Any] = {}
        if language:
            filters["language"] = language
        if min_stars is not None:
            filters["min_stars"] = min_stars
        if pushed_after:
            filters["pushed_after"] = pushed_after
        if topic:
            filters["topics"] = (topic,)
        parsed = rt.plugins.build_query(q, **filters)
        return {
            "github_query": parsed.github_query,
            "keywords": parsed.keywords,
            "qualifiers": parsed.qualifiers,
            "notes": parsed.notes,
        }

    @app.get("/api/search")
    async def search(
        q: str,
        limit: int = 10,
        language: str | None = None,
        min_stars: int | None = None,
        pushed_after: str | None = None,
        topic: str | None = None,
    ) -> dict[str, Any]:
        """远程搜索候选仓库并落库为**待审查**候选。

        未配置 GitHub 令牌时返回 503（``search_unavailable``），
        **绝不**返回空列表来假装"没有找到"。
        """
        filters: dict[str, Any] = {}
        if language:
            filters["language"] = language
        if min_stars is not None:
            filters["min_stars"] = min_stars
        if pushed_after:
            filters["pushed_after"] = pushed_after
        if topic:
            filters["topics"] = (topic,)

        if ":" in q and " " not in q.split(":", 1)[0]:
            # 已是检索式：直接透传
            items = rt.plugins.search_remote(q, limit=limit, actor="user")
            expr = q.strip()
        else:
            parsed = rt.plugins.build_query(q, **filters)
            items = rt.plugins.search_remote(parsed.github_query, limit=limit, actor="user")
            expr = parsed.github_query

        return {
            "query": expr,
            "count": len(items),
            "items": [dump_plugin(p) for p in items],
        }

    @app.get("/api/plugins/{plugin_id}/inspect")
    async def inspect_plugin(plugin_id: str) -> dict[str, Any]:
        """深度检查：固定 commit、README/测试/CI/依赖探测、Release 数。"""
        return dump_plugin(rt.plugins.inspect_remote(plugin_id, actor="user"))

    @app.get("/api/plugins/{plugin_id}/review")
    async def review_plugin(plugin_id: str, max_files: int = 20) -> dict[str, Any]:
        """对已固定 commit 的候选做静态安全审查（只读；不执行任何安装脚本）。"""
        capped = max(1, min(int(max_files), _MAX_REVIEW_FILES_CAP))
        return dump_plugin(rt.plugins.review(plugin_id, actor="user", max_files=capped))

    @app.get("/api/compare")
    async def compare(ids: str) -> dict[str, Any]:
        """对比多个候选（逗号分隔 ID）。输出证据缺口，不做"最佳推荐"。"""
        plugin_ids = [p.strip() for p in ids.split(",") if p.strip()]
        if not plugin_ids:
            raise ValidationError("ids 不能为空")
        return rt.plugins.compare(plugin_ids)

    @app.get("/api/tasks")
    async def tasks(limit: int = 50) -> dict[str, Any]:
        """任务/操作历史：直接来自审计日志（阶段 4 尚无独立任务表）。"""
        rows = rt.plugins.list_audit(limit=limit)
        items = [r.__dict__ for r in rows]
        return {"count": len(items), "items": items}

    # ------------------------------------------------------------------ #
    # 阶段 5：会话 / CSRF 与写操作（安装闭环）
    #
    # 写操作**必须**同时满足：
    #   1) 请求来自受信任网页（同源 Origin，由中间件保证）；
    #   2) 携带有效会话 Cookie（HttpOnly + SameSite=Strict）；
    #   3) 携带与该会话绑定的 CSRF 令牌（X-CSRF-Token）；
    #   4) 目标操作已由用户通过网页明确确认（一次性确认令牌）。
    # Agent / MCP 只能生成计划，**无法**自行确认或执行。
    # ------------------------------------------------------------------ #
    def _installs() -> InstallService:
        if rt.installs is None:
            raise InstallUnavailable("安装服务未装配")
        return rt.installs

    def _require_write_auth(request: Request) -> None:
        if not rt.settings.install_enabled:
            raise InstallUnavailable("安装写操作已在配置中禁用（MCPM_ENABLE_INSTALL=0）")
        rt.sessions.verify(
            request.cookies.get(SESSION_COOKIE),
            request.headers.get(CSRF_HEADER),
        )

    def _plugin_id_from(payload: dict[str, Any]) -> str:
        plugin_id = payload.get("plugin_id")
        if not isinstance(plugin_id, str) or not plugin_id.strip():
            raise ValidationError("plugin_id 不能为空")
        return plugin_id.strip()

    @app.get("/api/session")
    async def create_session() -> JSONResponse:
        """建立新会话并下发会话 Cookie 与 CSRF 令牌（明文仅此一次）。"""
        token, csrf = rt.sessions.create()
        resp = _json_response(
            200, {"ok": True, "ttl_seconds": rt.settings.session_ttl_seconds}
        )
        resp.set_cookie(
            SESSION_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=rt.settings.session_ttl_seconds,
        )
        resp.headers[CSRF_HEADER] = csrf
        return resp

    @app.get("/api/operations")
    async def list_operations(
        plugin_id: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """操作历史（只读）。"""
        ops = _installs().list(plugin_id=plugin_id, limit=limit)
        return {"count": len(ops), "items": [op.to_dict() for op in ops]}

    @app.get("/api/operations/{operation_id}")
    async def get_operation(operation_id: str) -> dict[str, Any]:
        """单个操作的详情 + 确认状态 + 步骤日志（只读）。"""
        svc = _installs()
        op = svc.get(operation_id)
        return {
            "operation": op.to_dict(),
            "confirmation": svc.confirmation(operation_id),
            "logs": [log.__dict__ for log in svc.logs(operation_id)],
        }

    @app.post("/api/install/plan")
    async def create_install_plan(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """生成安装计划（等待用户确认；**不写任何文件**）。"""
        _require_write_auth(request)
        plugin = rt.plugins.get(_plugin_id_from(payload))
        op = _installs().create_plan(plugin, action="install", actor="user")
        return op.to_dict()

    @app.post("/api/uninstall/plan")
    async def create_uninstall_plan(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """生成卸载计划（仅列出本系统登记并拥有的文件）。"""
        _require_write_auth(request)
        plugin = rt.plugins.get(_plugin_id_from(payload))
        op = _installs().create_plan(plugin, action="uninstall", actor="user")
        return op.to_dict()

    @app.post("/api/rollback/plan")
    async def create_rollback_plan(
        request: Request, payload: dict[str, Any] = Body(...)
    ) -> dict[str, Any]:
        """生成回滚计划（回滚最近一次成功安装）。"""
        _require_write_auth(request)
        plugin = rt.plugins.get(_plugin_id_from(payload))
        op = _installs().create_plan(plugin, action="rollback", actor="user")
        return op.to_dict()

    @app.post("/api/operations/{operation_id}/confirm")
    async def confirm_operation(request: Request, operation_id: str) -> dict[str, Any]:
        """用户确认：生成一次性确认令牌（绑定当前计划摘要）。"""
        _require_write_auth(request)
        svc = _installs()
        token = svc.confirm(operation_id, actor="user")
        return {
            "operation_id": operation_id,
            "confirmation_token": token,
            "confirmation": svc.confirmation(operation_id),
        }

    @app.post("/api/operations/{operation_id}/execute")
    async def execute_operation(
        request: Request,
        operation_id: str,
        payload: dict[str, Any] = Body(...),
    ) -> dict[str, Any]:
        """执行：校验确认令牌与当前计划一致后落地变更。"""
        _require_write_auth(request)
        token = payload.get("confirmation_token")
        if not isinstance(token, str) or not token:
            raise ValidationError("confirmation_token 不能为空")
        op = _installs().execute(operation_id, token=token, actor="user")
        return op.to_dict()

    @app.post("/api/operations/{operation_id}/cancel")
    async def cancel_operation(request: Request, operation_id: str) -> dict[str, Any]:
        """取消尚未执行的操作。"""
        _require_write_auth(request)
        return _installs().cancel(operation_id, actor="user").to_dict()

    # ------------------------------------------------------------------ #
    # 阶段 4：SSE 实时事件流（只读 GET）
    # ------------------------------------------------------------------ #
    @app.get("/api/events/stats")
    async def event_stats() -> dict[str, int]:
        return rt.events.stats()

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        """SSE 事件流。

        - 只推送**已脱敏**的审计/进度事件；不推送任何凭据；
        - 并发订阅有上限（超出返回 503）；
        - 定期发送心跳注释行，避免中间层判死连接。
        """
        sub = rt.events.subscribe()
        if sub is None:
            raise HTTPException(status_code=503, detail="订阅数已达上限")
        # 绑定当前事件循环，使跨线程发布也能投递。
        try:
            rt.events.bind_loop(asyncio.get_running_loop())
        except RuntimeError:  # pragma: no cover - 理论上不可达
            pass

        async def gen():
            try:
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(
                            sub.queue.get(), timeout=_SSE_HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    payload = json.dumps(event, ensure_ascii=False, default=str)
                    yield f"event: {event.get('type', 'message')}\ndata: {payload}\n\n"
            finally:
                rt.events.unsubscribe(sub)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    # ------------------------------------------------------------------ #
    # 静态页面
    # ------------------------------------------------------------------ #
    web_dir = rt.settings.web_dir

    @app.get("/")
    async def index() -> FileResponse:
        index_path = web_dir / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=500, detail="web/index.html 缺失")
        return FileResponse(index_path, media_type="text/html; charset=utf-8")

    if (web_dir / "assets").is_dir():
        app.mount(
            "/assets", StaticFiles(directory=str(web_dir / "assets")), name="assets"
        )

    return app
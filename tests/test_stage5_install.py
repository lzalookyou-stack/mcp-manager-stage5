"""阶段 5 测试：安全安装闭环。

覆盖：
- 会话 / CSRF 校验（拒绝一切不满足条件的写请求）；
- 计划生成的前置条件（必须有固定 commit、critical 发现必须拒绝）；
- 确认令牌（一次性、绑定计划摘要、过期失效）；
- 安装 / 回滚 / 卸载的文件系统行为与归属台账；
- 失败回滚、崩溃遗留事务标记为 interrupted（**绝不视为成功**）；
- 结构化命令执行（``shell=False``、拒绝字符串命令、越界 cwd 拒绝）；
- 网页写接口的完整授权链路与未授权拒绝。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import load_settings
from app.install import (
    Installer,
    InstallService,
    InstallServiceError,
    ManagedRoots,
    MemoryFileProvider,
    OperationStatus,
    OperationStore,
    run_command,
)
from app.install.confirmation import (
    ConfirmationError,
    create_confirmation,
    verify_confirmation,
)
from app.install.exec import CommandRejected
from app.install.plan import build_binding
from app.models import Plugin, PluginKind
from app.runtime import Runtime
from app.security import SecurityError
from app.session import SessionStore
from app.web import create_app

ALLOWED_HOST = "testserver"
ORIGIN = f"http://{ALLOWED_HOST}"

FILES = {
    "index.js": b"export const name = 'demo';\n",
    "package.json": b'{"name":"demo","version":"1.0.0"}\n',
    "lib/util.js": b"export const add = (a, b) => a + b;\n",
}


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
@pytest.fixture()
def roots(tmp_path: Path) -> ManagedRoots:
    return ManagedRoots.create(tmp_path / "data")


@pytest.fixture()
def installer(runtime, roots) -> Installer:
    def factory(plugin):  # type: ignore[no-untyped-def]
        return MemoryFileProvider(dict(FILES))

    return Installer(runtime.conn, roots, provider_factory=factory)


@pytest.fixture()
def installs(runtime, installer) -> InstallService:
    return InstallService(
        runtime.conn,
        installer,
        plugin_lookup=runtime.plugins.get,
        confirmation_ttl=600,
    )


def make_plugin(service, *, pinned_ref: str | None = "a" * 40, source: str = "github:acme/demo"):
    return service.upsert(
        Plugin.new(
            source=source,
            slug="demo",
            name="acme/demo",
            kind=PluginKind.MCP_SERVER,
            pinned_ref=pinned_ref,
        ),
        actor="user",
    )


# --------------------------------------------------------------------------- #
# 会话 / CSRF
# --------------------------------------------------------------------------- #
def test_session_roundtrip_and_rejections():
    store = SessionStore(ttl_seconds=60)
    token, csrf = store.create()
    store.verify(token, csrf)  # 不应抛异常

    with pytest.raises(Exception):
        store.verify(token, "wrong-csrf")
    with pytest.raises(Exception):
        store.verify("unknown-session", csrf)
    with pytest.raises(Exception):
        store.verify(None, csrf)
    with pytest.raises(Exception):
        store.verify(token, None)


def test_session_expires_after_ttl():
    store = SessionStore(ttl_seconds=1)
    token, csrf = store.create()
    # 把 last_seen 拨回过去，模拟空闲超时
    for session in store._sessions.values():
        session.last_seen = time.time() - 10
    with pytest.raises(Exception):
        store.verify(token, csrf)
    assert store.count() == 0


# --------------------------------------------------------------------------- #
# 计划生成
# --------------------------------------------------------------------------- #
def test_plan_requires_pinned_ref(installs, service):
    plugin = make_plugin(service, pinned_ref=None)
    with pytest.raises(Exception) as exc:
        installs.create_plan(plugin)
    assert "固定" in str(exc.value) or "commit" in str(exc.value)


def test_plan_rejects_vetoed_review(installs, service):
    from app.models import RiskLevel, SecurityReport

    plugin = service.upsert(
        Plugin.new(
            source="github:acme/demo",
            slug="demo",
            name="acme/demo",
            kind=PluginKind.MCP_SERVER,
            pinned_ref="a" * 40,
            security_report=SecurityReport(
                coverage="partial",
                pinned_ref="a" * 40,
                risk_level=RiskLevel.CRITICAL,
                vetoed=True,
                scanned_files=["install.sh"],
            ),
        ),
        actor="user",
    )
    with pytest.raises(Exception) as exc:
        installs.create_plan(plugin)
    assert "一票否决" in str(exc.value) or "critical" in str(exc.value).lower()


def test_plan_lists_files_and_is_awaiting_confirmation(installs, service):
    plugin = make_plugin(service)
    op = installs.create_plan(plugin)
    assert op.status == OperationStatus.AWAITING_CONFIRMATION.value
    plan = op.plan
    assert plan is not None
    assert plan.pinned_ref == "a" * 40
    assert {f.path for f in plan.files} == set(FILES)
    assert plan.commands == []           # 默认不执行任何安装脚本
    assert plan.spawns_process is False
    assert plan.plan_digest and len(plan.plan_digest) == 64
    assert plan.expires_at is not None
    # 计划阶段**不产生任何写入**：目标目录此时尚不存在
    assert not Path(plan.target_dir).exists()


# --------------------------------------------------------------------------- #
# 确认令牌
# --------------------------------------------------------------------------- #
def test_confirmation_is_single_use(installs, service, runtime):
    plugin = make_plugin(service)
    op = installs.create_plan(plugin)
    token = installs.confirm(op.id)

    binding = build_binding(op.id, op.plan)
    verify_confirmation(runtime.conn, operation_id=op.id, token=token, current_binding=binding)
    with pytest.raises(ConfirmationError):
        verify_confirmation(
            runtime.conn, operation_id=op.id, token=token, current_binding=binding
        )


def test_confirmation_rejects_plan_change(runtime):
    from app.install.plan import InstallPlan, PlannedFile, default_expiry

    plan_a = InstallPlan(
        plugin_id="p1",
        plugin_name="n",
        kind="mcp_server",
        source="github:a/b",
        pinned_ref="c" * 40,
        target_dir="/tmp/x",
        files=[PlannedFile(path="a.js", action="create", size=1)],
        expires_at=default_expiry(600),
    ).sealed()
    op_id = "op-1"
    token, _ = create_confirmation(
        runtime.conn, operation_id=op_id, binding=build_binding(op_id, plan_a), ttl_seconds=600
    )

    # 计划变化：文件清单不同 → 绑定摘要不同 → 旧确认必须失效
    plan_b = plan_a.model_copy(
        update={"files": [PlannedFile(path="b.js", action="create", size=1)]}
    ).sealed()
    with pytest.raises(ConfirmationError):
        verify_confirmation(
            runtime.conn,
            operation_id=op_id,
            token=token,
            current_binding=build_binding(op_id, plan_b),
        )


def test_confirmation_rejects_expired(runtime):
    from app.install.plan import InstallPlan, default_expiry

    plan = InstallPlan(
        plugin_id="p1",
        plugin_name="n",
        kind="mcp_server",
        source="github:a/b",
        pinned_ref="c" * 40,
        target_dir="/tmp/x",
        expires_at=default_expiry(600),
    ).sealed()
    token, _ = create_confirmation(
        runtime.conn, operation_id="op-2", binding=build_binding("op-2", plan), ttl_seconds=60
    )
    from datetime import datetime, timedelta, timezone

    with pytest.raises(ConfirmationError):
        verify_confirmation(
            runtime.conn,
            operation_id="op-2",
            token=token,
            current_binding=build_binding("op-2", plan),
            now=datetime.now(timezone.utc) + timedelta(hours=1),
        )


# --------------------------------------------------------------------------- #
# 执行 / 回滚 / 卸载
# --------------------------------------------------------------------------- #
def test_install_writes_files_and_records_ownership(installs, service, roots):
    plugin = make_plugin(service)
    op = installs.create_plan(plugin)
    token = installs.confirm(op.id)
    done = installs.execute(op.id, token=token)

    assert done.status == OperationStatus.SUCCEEDED.value
    target = roots.plugin_dir(plugin.id)
    assert (target / "index.js").read_bytes() == FILES["index.js"]
    assert (target / "lib" / "util.js").read_bytes() == FILES["lib/util.js"]
    assert (roots.config_path(plugin.id)).is_file()
    assert done.result["applied"]["verified"] is True

    from app.install import owned_paths

    owned = owned_paths(installs._conn, plugin.id)
    assert str(target / "index.js") in owned


def test_execute_requires_confirm_token(installs, service):
    plugin = make_plugin(service)
    op = installs.create_plan(plugin)
    with pytest.raises(Exception):
        installs.execute(op.id, token="bogus")
    # 状态仍为 awaiting_confirmation，未被误推进
    assert installs.get(op.id).status == OperationStatus.AWAITING_CONFIRMATION.value


def test_execute_twice_rejected(installs, service):
    plugin = make_plugin(service)
    op = installs.create_plan(plugin)
    token = installs.confirm(op.id)
    installs.execute(op.id, token=token)
    with pytest.raises(InstallServiceError):
        installs.execute(op.id, token=token)


def test_uninstall_removes_only_owned(installs, service, roots, tmp_path):
    plugin = make_plugin(service)
    op = installs.create_plan(plugin)
    token = installs.confirm(op.id)
    installs.execute(op.id, token=token)

    target = roots.plugin_dir(plugin.id)
    outsider = target / "user-added.txt"
    outsider.write_text("不属于本系统\n", encoding="utf-8")

    # 归属台账里塞一条越界路径，卸载**绝不能**删除它
    danger = tmp_path / "outside.txt"
    danger.write_text("不要删我\n", encoding="utf-8")
    from app.install import record_ownership

    record_ownership(
        installs._conn,
        plugin_id=plugin.id,
        operation_id="fake-op",
        files=[(str(danger), "create", None)],
    )

    plan = installs.create_plan(plugin, action="uninstall")
    t2 = installs.confirm(plan.id)
    done = installs.execute(plan.id, token=t2)
    assert done.status == OperationStatus.SUCCEEDED.value
    assert danger.exists(), "越界路径绝不能被删除"
    assert not (target / "index.js").exists()


def test_rollback_restores_previous_content(installs, service, roots):
    plugin = make_plugin(service)
    op = installs.create_plan(plugin)
    token = installs.confirm(op.id)
    installs.execute(op.id, token=token)

    target = roots.plugin_dir(plugin.id)
    (target / "index.js").write_text("被用户改坏了\n", encoding="utf-8")

    rb = installs.create_plan(plugin, action="rollback")
    t2 = installs.confirm(rb.id)
    done = installs.execute(rb.id, token=t2)
    assert done.status == OperationStatus.SUCCEEDED.value
    # 回滚后该插件的受管文件已被清除（安装前不存在 → 直接删除）
    assert not (target / "index.js").exists()


def test_install_failure_marks_failed_and_restores(runtime, roots, service):
    """写入过程中失败：操作必须记为 failed，且不留半成品。"""

    class ExplodingProvider(MemoryFileProvider):
        def read(self, path: str) -> bytes:
            if path == "lib/util.js":
                from app.install.provider import ProviderError

                raise ProviderError("模拟网络中断")
            return super().read(path)

    installer = Installer(
        runtime.conn, roots, provider_factory=lambda plugin: ExplodingProvider(dict(FILES))
    )
    svc = InstallService(runtime.conn, installer, plugin_lookup=runtime.plugins.get)
    plugin = make_plugin(service)
    op = svc.create_plan(plugin)
    token = svc.confirm(op.id)

    with pytest.raises(Exception):
        svc.execute(op.id, token=token)

    after = svc.get(op.id)
    assert after.status == OperationStatus.FAILED.value
    assert after.error
    assert not roots.plugin_dir(plugin.id).exists(), "失败后不得残留半成品"


def test_interrupted_transactions_never_marked_succeeded(runtime):
    store = OperationStore(runtime.conn)
    op = store.create(
        plugin_id="p1",
        action="install",
        actor="user",
        status=OperationStatus.RUNNING,
    )
    interrupted = store.mark_interrupted()
    assert op.id in interrupted
    assert store.get(op.id).status == OperationStatus.INTERRUPTED.value


# --------------------------------------------------------------------------- #
# 结构化命令执行
# --------------------------------------------------------------------------- #
def test_run_command_rejects_string_and_shell_metacharacters(tmp_path: Path):
    with pytest.raises(CommandRejected):
        run_command("rm -rf /", cwd=tmp_path, allowed_roots=[tmp_path])  # type: ignore[arg-type]


def test_run_command_rejects_cwd_outside_roots(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    with pytest.raises(CommandRejected):
        run_command(["/bin/true"], cwd=tmp_path, allowed_roots=[allowed])


def test_run_command_executes_without_shell(tmp_path: Path):
    """``shell=False``：``;`` 不会被解释为命令分隔符。"""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    marker = allowed / "pwned"
    result = run_command(
        ["/bin/echo", f"hi; touch {marker}"],
        cwd=allowed,
        allowed_roots=[allowed],
    )
    assert result.ok
    assert not marker.exists(), "分号不得被 shell 解释"
    assert ";" in result.stdout


def test_run_command_times_out(tmp_path: Path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    result = run_command(
        ["/bin/sleep", "5"], cwd=allowed, allowed_roots=[allowed], timeout_s=0.2
    )
    assert result.timed_out
    assert not result.ok


def test_managed_roots_reject_escape(tmp_path: Path):
    roots = ManagedRoots.create(tmp_path / "data")
    with pytest.raises(SecurityError):
        roots.assert_managed(tmp_path / "outside")


# --------------------------------------------------------------------------- #
# 网页授权链路
# --------------------------------------------------------------------------- #
@pytest.fixture()
def web(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MCPM_ALLOWED_HOSTS", ALLOWED_HOST)
    monkeypatch.setenv("MCPM_ALLOWED_ORIGINS", ORIGIN)
    s = load_settings(
        host="127.0.0.1",
        port=8765,
        db_path=tmp_path / "web.db",
        data_dir=tmp_path / "data",
    )
    rt = Runtime.create(s)

    # 用内存文件来源替换真实 GitHub 来源，避免测试触网
    roots = ManagedRoots.create(s.data_dir)
    installer = Installer(
        rt.conn, roots, provider_factory=lambda plugin: MemoryFileProvider(dict(FILES))
    )
    rt.installs = InstallService(
        rt.conn, installer, plugin_lookup=rt.plugins.get, confirmation_ttl=600
    )
    app = create_app(rt)
    with TestClient(app, base_url=ORIGIN) as c:
        c.runtime = rt  # type: ignore[attr-defined]
        yield c
    rt.close()


def _login(client: TestClient) -> str:
    resp = client.get("/api/session")
    assert resp.status_code == 200
    return resp.headers["X-CSRF-Token"]


def _seed(client: TestClient) -> str:
    rt = client.runtime  # type: ignore[attr-defined]
    p = rt.plugins.upsert(
        Plugin.new(
            source="github:acme/demo",
            slug="demo",
            name="acme/demo",
            kind=PluginKind.MCP_SERVER,
            pinned_ref="a" * 40,
        ),
        actor="user",
    )
    return p.id


def test_web_write_without_session_rejected(web: TestClient):
    pid = _seed(web)
    resp = web.post(
        "/api/install/plan", headers={"Origin": ORIGIN}, json={"plugin_id": pid}
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "session_rejected"


def test_web_write_with_session_but_wrong_csrf_rejected(web: TestClient):
    pid = _seed(web)
    _login(web)
    resp = web.post(
        "/api/install/plan",
        headers={"Origin": ORIGIN, "X-CSRF-Token": "wrong"},
        json={"plugin_id": pid},
    )
    assert resp.status_code == 403


def test_web_post_without_origin_rejected(web: TestClient):
    pid = _seed(web)
    _login(web)
    resp = web.post("/api/install/plan", json={"plugin_id": pid})
    assert resp.status_code == 403
    assert resp.json()["error"] == "csrf_origin_rejected"


def test_web_full_install_flow(web: TestClient):
    """完整链路：会话 → 计划 → 确认 → 执行。"""
    pid = _seed(web)
    csrf = _login(web)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}

    resp = web.post("/api/install/plan", headers=headers, json={"plugin_id": pid})
    assert resp.status_code == 200, resp.text
    op = resp.json()
    assert op["status"] == "awaiting_confirmation"

    resp = web.post(f"/api/operations/{op['id']}/confirm", headers=headers)
    assert resp.status_code == 200, resp.text
    token = resp.json()["confirmation_token"]

    resp = web.post(
        f"/api/operations/{op['id']}/execute",
        headers=headers,
        json={"confirmation_token": token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "succeeded"

    rt = web.runtime  # type: ignore[attr-defined]
    target = ManagedRoots.create(rt.settings.data_dir).plugin_dir(pid)
    assert (target / "index.js").is_file()

    # 已执行的令牌不可重复使用
    resp = web.post(
        f"/api/operations/{op['id']}/execute",
        headers=headers,
        json={"confirmation_token": token},
    )
    assert resp.status_code in (409, 400)


def test_web_agent_cannot_confirm_without_page_session(web: TestClient):
    """Agent 侧（无会话）无法确认，也无法执行：必须 403。"""
    pid = _seed(web)
    csrf = _login(web)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    op = web.post(
        "/api/install/plan", headers=headers, json={"plugin_id": pid}
    ).json()

    bare = {"Origin": ORIGIN}
    assert web.post(f"/api/operations/{op['id']}/confirm", headers=bare).status_code == 403
    assert (
        web.post(
            f"/api/operations/{op['id']}/execute",
            headers=bare,
            json={"confirmation_token": "x"},
        ).status_code
        == 403
    )


def test_web_operation_history_is_readonly(web: TestClient):
    pid = _seed(web)
    csrf = _login(web)
    headers = {"Origin": ORIGIN, "X-CSRF-Token": csrf}
    op = web.post(
        "/api/install/plan", headers=headers, json={"plugin_id": pid}
    ).json()

    resp = web.get("/api/operations")
    assert resp.status_code == 200
    assert resp.json()["count"] >= 1

    resp = web.get(f"/api/operations/{op['id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["operation"]["id"] == op["id"]
    assert "logs" in body
    # 只读视图不得泄露确认令牌明文
    assert "token" not in (body["confirmation"] or {})

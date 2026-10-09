"""服务层测试：幂等写入、查询、审计、未实现能力必须显式失败。"""

from __future__ import annotations

import pytest

from app.models import InstallStatus, Plugin, PluginKind, RiskLevel
from app.services import (
    PluginNotFound,
    PluginService,
    SearchUnavailable,
    ValidationError,
)


def test_upsert_then_get_roundtrip(service: PluginService):
    p = Plugin.new(source="github:o/r", slug="s", name="测试插件", kind=PluginKind.MCP_SERVER)
    service.upsert(p, actor="user")

    got = service.get(p.id)
    assert got.id == p.id
    assert got.name == "测试插件"


def test_upsert_is_idempotent_by_stable_id(service: PluginService):
    p1 = Plugin.new(source="github:o/r", slug="s", name="旧名", kind=PluginKind.MCP_SERVER)
    service.upsert(p1, actor="user")
    assert service.count() == 1

    p2 = Plugin.new(source="github:o/r", slug="s", name="新名", kind=PluginKind.MCP_SERVER)
    service.upsert(p2, actor="user")
    assert service.count() == 1, "同一 (source, slug) 必须复用同一条目"
    assert service.get(p1.id).name == "新名"


def test_get_missing_raises(service: PluginService):
    with pytest.raises(PluginNotFound):
        service.get("github:nope/nope:000000000000")


def test_list_filters(service: PluginService):
    service.upsert(
        Plugin.new(source="github:a/a", slug="one", name="A", kind=PluginKind.MCP_SERVER),
        actor="user",
    )
    service.upsert(
        Plugin.new(source="github:b/b", slug="two", name="B", kind=PluginKind.SKILL),
        actor="user",
    )

    assert len(service.list()) == 2
    assert len(service.list(kind=PluginKind.SKILL)) == 1
    assert service.list(kind=PluginKind.SKILL)[0].name == "B"
    assert len(service.list(risk_level=RiskLevel.HIGH)) == 0


def test_list_pagination(service: PluginService):
    for i in range(5):
        service.upsert(
            Plugin.new(
                source=f"github:o/r{i}",
                slug=f"s{i}",
                name=f"P{i}",
                kind=PluginKind.MCP_SERVER,
            ),
            actor="user",
        )
    page1 = service.list(limit=2, offset=0)
    page2 = service.list(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {p.id for p in page1}.isdisjoint({p.id for p in page2})


def test_audit_records_actor_and_outcome(service: PluginService):
    service.audit(actor="agent", action="probe", outcome="denied", detail="测试")
    rows = service.list_audit(limit=10)
    assert rows
    assert rows[0].actor == "agent"
    assert rows[0].outcome == "denied"


def test_audit_rejects_bad_actor(service: PluginService):
    with pytest.raises(ValidationError):
        service.audit(actor="root", action="x")
    with pytest.raises(ValidationError):
        service.audit(actor="user", action="x", outcome="maybe")


def test_audit_is_written_on_upsert(service: PluginService):
    p = Plugin.new(source="github:o/r", slug="s", name="N", kind=PluginKind.MCP_SERVER)
    service.upsert(p, actor="system")
    rows = service.list_audit()
    assert any(r.action == "plugin.upsert" and r.target == p.id for r in rows)


def test_register_placeholder_is_pending_not_approved(service: PluginService):
    p = service.register_placeholder(
        source="github:o/r",
        slug="newthing",
        name="新条目",
        kind=PluginKind.AGENT_PLUGIN,
        actor="user",
    )
    assert p.review_status.value == "pending"
    assert p.install_status is InstallStatus.NOT_INSTALLED


def test_register_placeholder_rejects_bad_slug(service: PluginService):
    with pytest.raises(Exception):
        service.register_placeholder(
            source="github:o/r",
            slug="../escape",
            name="坏条目",
            kind=PluginKind.AGENT_PLUGIN,
            actor="user",
        )


def test_stats(service: PluginService):
    service.upsert(
        Plugin.new(source="github:a/a", slug="one", name="A", kind=PluginKind.MCP_SERVER),
        actor="user",
    )
    stats = service.stats()
    assert stats["total"] == 1
    assert stats["by_kind"]["mcp_server"] == 1
    assert stats["by_install_status"]["not_installed"] == 1


@pytest.mark.parametrize("method", ["install", "rollback", "uninstall"])
def test_write_methods_absent_from_plugin_service(
    service: PluginService, method: str
):
    """写操作**刻意不**暴露在 PluginService 上（阶段 5 的设计变更）。

    安装 / 回滚 / 卸载必须经 ``InstallService`` 的
    「生成计划 → 用户通过受信任网页确认 → 校验令牌 → 执行」流程。
    直接在 PluginService 上提供写方法会形成绕过用户授权的旁路，因此这里断言
    这些方法**不存在**（比抛 ``NotImplementedError`` 更强的保证）。
    """
    assert not hasattr(service, method), (
        f"PluginService 不应提供 {method}；写操作必须经 InstallService 编排"
    )


def test_search_without_github_client_fails_loudly(service: PluginService):
    """未配置 GitHub 客户端时，搜索必须显式失败，**绝不**返回空列表。

    返回空列表会被调用方误读为"没有搜索结果"，属于伪成功。
    """
    assert service._github is None
    with pytest.raises(SearchUnavailable):
        service.search_remote("memory")


def test_score_missing_plugin_raises(service: PluginService):
    with pytest.raises(PluginNotFound):
        service.score("some-id")


def test_review_missing_plugin_raises(service: PluginService):
    with pytest.raises(PluginNotFound):
        service.review("some-id")


def test_install_service_is_the_only_write_path(runtime):
    """写操作的唯一入口是 ``Runtime.installs``（InstallService）。"""
    from app.services import InstallService

    assert isinstance(runtime.installs, InstallService)
    assert not hasattr(runtime.plugins, "install")
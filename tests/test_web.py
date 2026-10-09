"""Web 层测试：安全中间件、只读 API、无写接口。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models import Plugin, PluginKind
from app.web import create_app

# 测试客户端的默认 base_url 让 Host 头为 "testserver"，需显式加入允许列表
ALLOWED_HOST = "testserver"
ORIGIN = f"http://{ALLOWED_HOST}"


@pytest.fixture()
def client(settings, monkeypatch):
    """独立的 TestClient。每个测试用独立 DB，避免与 runtime 夹具互相干扰。"""
    monkeypatch.setenv("MCPM_ALLOWED_HOSTS", ALLOWED_HOST)
    monkeypatch.setenv("MCPM_ALLOWED_ORIGINS", ORIGIN)
    from app.config import load_settings
    from app.runtime import Runtime

    s = load_settings(
        host="127.0.0.1",
        port=8765,
        db_path=settings.db_path,
    )
    rt = Runtime.create(s)
    app = create_app(rt)
    with TestClient(app, base_url=ORIGIN) as c:
        c.runtime = rt  # type: ignore[attr-defined]
        yield c
    rt.close()


def test_health_ok(client: TestClient):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["loopback_only"] is True


def test_index_serves_html(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "mcp-manager" in resp.text


def test_security_headers_present(client: TestClient):
    resp = client.get("/api/health")
    for header in (
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Cross-Origin-Opener-Policy",
    ):
        assert header in resp.headers, header
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "'unsafe-inline'" not in csp, "CSP 不得放开内联脚本"


def test_host_header_rejected(client: TestClient):
    resp = client.get("/api/health", headers={"Host": "evil.example.com"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_host"


def test_origin_required_for_unsafe_method(client: TestClient):
    """阶段 2 没有任何写接口，但中间件仍必须先拒掉无 Origin 的写请求。"""
    resp = client.post("/api/health")
    assert resp.status_code == 403
    assert resp.json()["error"] == "csrf_origin_rejected"


def test_bad_origin_rejected(client: TestClient):
    resp = client.post(
        "/api/health",
        headers={"Origin": "http://attacker.example"},
    )
    assert resp.status_code == 403


def test_plugins_list_empty(client: TestClient):
    resp = client.get("/api/plugins")
    assert resp.status_code == 200
    assert resp.json() == {"count": 0, "items": []}


def test_plugins_list_after_insert(client: TestClient):
    rt = client.runtime  # type: ignore[attr-defined]
    rt.plugins.upsert(
        Plugin.new(
            source="github:o/r",
            slug="demo",
            name="演示",
            kind=PluginKind.MCP_SERVER,
        ),
        actor="user",
    )
    resp = client.get("/api/plugins")
    body = resp.json()
    assert body["count"] == 1
    assert body["items"][0]["name"] == "演示"
    assert body["items"][0]["kind"] == "mcp_server"


def test_plugins_unknown_kind_rejected(client: TestClient):
    resp = client.get("/api/plugins?kind=not-a-kind")
    assert resp.status_code == 400


def test_plugin_detail_404(client: TestClient):
    # ID 形如 {source}:{slug}:{hash12}，其中 / 已被规整为 -，故不含斜杠
    resp = client.get("/api/plugins/github-nope-nope:nope:000000000000")
    assert resp.status_code == 404
    assert resp.json()["error"] == "plugin_not_found"


def test_plugin_detail_ok(client: TestClient):
    rt = client.runtime  # type: ignore[attr-defined]
    p = rt.plugins.upsert(
        Plugin.new(source="github:o/r", slug="demo", name="演示", kind=PluginKind.SKILL),
        actor="user",
    )
    resp = client.get(f"/api/plugins/{p.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == p.id


def test_audit_endpoint(client: TestClient):
    resp = client.get("/api/audit?limit=5")
    assert resp.status_code == 200
    assert "items" in resp.json()


def test_write_endpoints_require_authorization(client: TestClient):
    """阶段 5 引入写接口后的负向断言（**同步更新，而非删除**）：

    - 旧的非写路径仍然**不存在**（404/405）；
    - 真实的写接口在未携带会话 Cookie / CSRF 令牌时**必须被拒绝**（403），绝不放行。
    """
    rt = client.runtime  # type: ignore[attr-defined]
    p = rt.plugins.upsert(
        Plugin.new(source="github:o/r", slug="demo", name="演示", kind=PluginKind.SKILL),
        actor="user",
    )
    for path in (
        "/api/plugins",
        f"/api/plugins/{p.id}/install",
        f"/api/plugins/{p.id}/approve",
        "/api/install",
        "/api/search",
    ):
        resp = client.post(path, headers={"Origin": ORIGIN}, json={})
        assert resp.status_code in (404, 405), f"{path} 不应存在写接口，实际 {resp.status_code}"

    # 真实存在的写接口：没有会话 + CSRF 时必须是 403，不能执行任何写入。
    resp = client.post(
        "/api/install/plan", headers={"Origin": ORIGIN}, json={"plugin_id": p.id}
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == "session_rejected"


def test_stats_endpoint(client: TestClient):
    resp = client.get("/api/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"total", "by_kind", "by_install_status"}


def test_openapi_disabled(client: TestClient):
    """关闭交互式文档，减少攻击面。"""
    assert client.get("/openapi.json").status_code == 404
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
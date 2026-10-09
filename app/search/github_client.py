"""GitHub API 客户端（阶段 3）。

纪律（贯穿全项目）：
- 限流 / 网络失败 / 权限不足 / 仓库不存在**必须显式抛出** ``GitHubError``，
  **绝不返回伪造或缓存兜底的结果**；调用方负责把错误如实展示给用户。
- 所有请求携带令牌：匿名调用在共享出口 IP 下极易被限流（阶段 1 已验证）。
- **令牌绝不写入日志或异常消息**。
- 只做读取（GET）；本模块不产生任何写操作。

依赖：仅标准库 ``urllib``（见 docs/architecture.md 技术选型：不引入额外 HTTP 库）。
"""

from __future__ import annotations

import base64
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

API_ROOT = "https://api.github.com"
DEFAULT_TIMEOUT = 20.0
MAX_TEXT_BYTES = 512 * 1024  # 单个文件最多读取 512 KiB，超出即截断并标注


class GitHubError(RuntimeError):
    """GitHub 调用失败。

    ``kind`` 取值：
    ``rate_limited`` / ``not_found`` / ``unauthorized`` / ``invalid`` /
    ``network`` / ``server`` / ``error``。
    """

    def __init__(
        self,
        message: str,
        *,
        kind: str = "error",
        status: int | None = None,
        url: str | None = None,
        rate_limit: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.url = url
        self.rate_limit = rate_limit or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "github_error",
            "kind": self.kind,
            "status": self.status,
            "detail": str(self),
            "rate_limit": self.rate_limit,
        }


@dataclass(frozen=True)
class RepoInfo:
    """仓库概览（字段直接来自 API，缺失即为 None 并记录在 missing_fields）。"""

    full_name: str
    owner: str
    name: str
    html_url: str
    description: str | None
    default_branch: str
    stars: int
    forks: int
    open_issues: int
    language: str | None
    license_spdx: str | None
    license_name: str | None
    archived: bool
    pushed_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    topics: list[str] = field(default_factory=list)
    size_kb: int = 0
    missing_fields: list[str] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class RepositoryFile:
    """仓库内一个文本文件。"""

    path: str
    content: str
    size: int
    truncated: bool = False
    sha: str | None = None


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _rate_limit_from_headers(headers: Any) -> dict[str, Any]:
    def _int(name: str) -> int | None:
        raw = headers.get(name)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    return {
        "limit": _int("X-RateLimit-Limit"),
        "remaining": _int("X-RateLimit-Remaining"),
        "reset": _int("X-RateLimit-Reset"),
        "retry_after": _int("Retry-After"),
    }


class GitHubClient:
    """最小可用的 GitHub 只读客户端。

    ``transport`` 参数用于测试注入：签名为 ``(url, headers) -> (status, headers, body_bytes)``。
    传入自定义 transport 时**不会**发起真实网络请求。
    """

    def __init__(
        self,
        token: str | None = None,
        *,
        api_root: str = API_ROOT,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str = "mcp-manager/0.3",
        transport: Any = None,
    ) -> None:
        self._token = (token or "").strip() or None
        self._api_root = api_root.rstrip("/")
        self._timeout = timeout
        self._user_agent = user_agent
        self._transport = transport

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #
    @property
    def has_token(self) -> bool:
        return self._token is not None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _http_get(self, url: str, headers: dict[str, str]) -> tuple[int, Any, bytes]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as exc:
            body = b""
            try:
                body = exc.read()
            except Exception:  # pragma: no cover - 读取失败不致命
                body = b""
            return exc.code, exc.headers, body
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise GitHubError(
                f"网络请求失败：{type(exc).__name__}", kind="network", url=url
            ) from exc

    def _request(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self._api_root}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"

        transport = self._transport or self._http_get
        status, headers, body = transport(url, self._headers())
        rate_limit = _rate_limit_from_headers(headers)

        if status == 200:
            try:
                return json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GitHubError(
                    "响应不是合法 JSON", kind="server", status=status, url=url
                ) from exc

        message = self._extract_message(body) or f"HTTP {status}"

        if status == 404:
            raise GitHubError(
                f"资源不存在：{message}", kind="not_found", status=status,
                url=url, rate_limit=rate_limit,
            )
        if status == 401:
            raise GitHubError(
                "认证失败（令牌无效或已过期）", kind="unauthorized", status=status,
                url=url, rate_limit=rate_limit,
            )
        if status == 403 and rate_limit.get("remaining") == 0:
            raise GitHubError(
                f"触发 GitHub 限流（剩余 0，重置时间戳 {rate_limit.get('reset')}）",
                kind="rate_limited", status=status, url=url, rate_limit=rate_limit,
            )
        if status == 403:
            raise GitHubError(
                f"访问被拒绝：{message}", kind="unauthorized", status=status,
                url=url, rate_limit=rate_limit,
            )
        if status == 422:
            raise GitHubError(
                f"请求不合法：{message}", kind="invalid", status=status,
                url=url, rate_limit=rate_limit,
            )
        if status == 429:
            raise GitHubError(
                f"触发二级限流：{message}", kind="rate_limited", status=status,
                url=url, rate_limit=rate_limit,
            )
        if status >= 500:
            raise GitHubError(
                f"GitHub 服务端错误：{message}", kind="server", status=status,
                url=url, rate_limit=rate_limit,
            )
        raise GitHubError(
            f"未预期的响应：{message}", kind="error", status=status, url=url,
            rate_limit=rate_limit,
        )

    @staticmethod
    def _extract_message(body: bytes) -> str | None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            return None
        if isinstance(payload, dict) and isinstance(payload.get("message"), str):
            return payload["message"]
        return None

    # ------------------------------------------------------------------ #
    # 高层能力
    # ------------------------------------------------------------------ #
    def search_repositories(
        self,
        query: str,
        *,
        sort: str = "stars",
        order: str = "desc",
        per_page: int = 10,
        page: int = 1,
    ) -> list[RepoInfo]:
        if not query or not query.strip():
            raise GitHubError("搜索式不能为空", kind="invalid")
        per_page = max(1, min(int(per_page), 100))
        payload = self._request(
            "/search/repositories",
            {"q": query, "sort": sort, "order": order, "per_page": per_page, "page": page},
        )
        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise GitHubError("搜索响应缺少 items 字段", kind="server")
        return [self._to_repo_info(it) for it in items if isinstance(it, dict)]

    def get_repository(self, owner: str, repo: str) -> RepoInfo:
        payload = self._request(f"/repos/{owner}/{repo}")
        if not isinstance(payload, dict):
            raise GitHubError("仓库响应不是对象", kind="server")
        return self._to_repo_info(payload)

    def get_latest_commit(self, owner: str, repo: str, *, ref: str | None = None) -> str:
        payload = self._request(
            f"/repos/{owner}/{repo}/commits", {"per_page": 1, "sha": ref}
        )
        if not isinstance(payload, list) or not payload:
            raise GitHubError("无法获取提交列表（可能为空仓库）", kind="not_found")
        sha = payload[0].get("sha") if isinstance(payload[0], dict) else None
        if not isinstance(sha, str) or len(sha) != 40:
            raise GitHubError("提交 SHA 形态异常", kind="server")
        return sha.lower()

    def list_releases(self, owner: str, repo: str, *, per_page: int = 5) -> list[dict[str, Any]]:
        payload = self._request(f"/repos/{owner}/{repo}/releases", {"per_page": per_page})
        if not isinstance(payload, list):
            raise GitHubError("releases 响应不是数组", kind="server")
        return [r for r in payload if isinstance(r, dict)]

    def list_tree(
        self, owner: str, repo: str, ref: str, *, recursive: bool = True
    ) -> list[dict[str, Any]]:
        """列出固定 ref 下的完整文件树（git trees API）。

        返回原始条目列表（``path`` / ``type`` / ``size`` / ``sha``）；
        调用方负责按 ``type == "blob"`` 过滤。``truncated`` 为真时**显式抛出**，
        避免把不完整清单当成完整清单使用。
        """
        payload = self._request(
            f"/repos/{owner}/{repo}/git/trees/{ref}",
            {"recursive": "1" if recursive else None},
        )
        if not isinstance(payload, dict):
            raise GitHubError("trees 响应不是对象", kind="server")
        if payload.get("truncated") is True:
            raise GitHubError(
                "文件树过大，GitHub 返回了截断结果；拒绝基于不完整清单安装。",
                kind="invalid",
            )
        tree = payload.get("tree")
        if not isinstance(tree, list):
            raise GitHubError("trees 响应缺少 tree 字段", kind="server")
        return [e for e in tree if isinstance(e, dict)]

    def list_directory(self, owner: str, repo: str, path: str = "", *, ref: str | None = None) -> list[str]:
        """列出目录下的条目名（不含内容）。"""
        payload = self._request(f"/repos/{owner}/{repo}/contents/{path}", {"ref": ref})
        if isinstance(payload, dict):  # 传入的是文件
            return [str(payload.get("name", ""))]
        if not isinstance(payload, list):
            raise GitHubError("contents 响应形态异常", kind="server")
        return [str(e.get("name", "")) for e in payload if isinstance(e, dict)]

    def get_file(self, owner: str, repo: str, path: str, *, ref: str | None = None) -> RepositoryFile:
        """读取单个文本文件。超过 ``MAX_TEXT_BYTES`` 时截断并标注。"""
        payload = self._request(f"/repos/{owner}/{repo}/contents/{path}", {"ref": ref})
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GitHubError(f"不是文件：{path}", kind="not_found")
        encoded = payload.get("content")
        size = int(payload.get("size") or 0)
        if not isinstance(encoded, str):
            raise GitHubError(f"文件内容不可用（可能是二进制或过大）：{path}", kind="server")
        try:
            raw = base64.b64decode(encoded, validate=False)
        except Exception as exc:
            raise GitHubError(f"文件内容解码失败：{path}", kind="server") from exc

        truncated = len(raw) > MAX_TEXT_BYTES
        if truncated:
            raw = raw[:MAX_TEXT_BYTES]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise GitHubError(f"文件不是 UTF-8 文本：{path}", kind="server") from None

        return RepositoryFile(
            path=path, content=text, size=size, truncated=truncated,
            sha=payload.get("sha") if isinstance(payload.get("sha"), str) else None,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_repo_info(item: dict[str, Any]) -> RepoInfo:
        missing: list[str] = []

        def _get(key: str) -> Any:
            if key not in item or item[key] is None:
                missing.append(key)
                return None
            return item[key]

        license_obj = item.get("license")
        spdx = name = None
        if isinstance(license_obj, dict):
            spdx = license_obj.get("spdx_id")
            name = license_obj.get("name")
            if spdx in ("NOASSERTION", ""):
                spdx = None
        else:
            missing.append("license")

        owner = (item.get("owner") or {}).get("login") if isinstance(item.get("owner"), dict) else None
        if not owner:
            missing.append("owner.login")

        full_name = item.get("full_name")
        if not full_name:
            missing.append("full_name")

        return RepoInfo(
            full_name=str(full_name or ""),
            owner=str(owner or ""),
            name=str(item.get("name") or ""),
            html_url=str(item.get("html_url") or ""),
            description=item.get("description"),
            default_branch=str(item.get("default_branch") or "main"),
            stars=int(item.get("stargazers_count") or 0),
            forks=int(item.get("forks_count") or 0),
            open_issues=int(item.get("open_issues_count") or 0),
            language=item.get("language"),
            license_spdx=spdx,
            license_name=name,
            archived=bool(item.get("archived")),
            pushed_at=_parse_dt(item.get("pushed_at")),
            created_at=_parse_dt(item.get("created_at")),
            updated_at=_parse_dt(item.get("updated_at")),
            topics=list(item.get("topics") or []),
            size_kb=int(item.get("size") or 0),
            missing_fields=missing,
            raw=item,
        )

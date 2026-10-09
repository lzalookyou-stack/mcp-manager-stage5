"""运行期容器。

把 ``Settings`` / SQLite 连接 / ``EventBus`` / ``PluginService``（含 GitHub 客户端）
组装成单一对象，供网页与 MCP 两侧共用（**同一个 service 实例语义**）。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field

from app.config import Settings, load_settings
from app.db import open_database
from app.events import EventBus
from app.install import Installer, ManagedRoots
from app.install.provider import GitHubFileProvider
from app.install.service import InstallService, InstallUnavailable
from app.search.github_client import GitHubClient
from app.security import require_github_source
from app.services import PluginService
from app.session import SessionStore


@dataclass
class Runtime:
    settings: Settings
    conn: sqlite3.Connection
    plugins: PluginService
    events: EventBus = field(default_factory=EventBus)
    sessions: SessionStore = field(default_factory=SessionStore)
    installs: InstallService | None = None

    @classmethod
    def create(
        cls,
        settings: Settings | None = None,
        *,
        github: GitHubClient | None = None,
        events: EventBus | None = None,
    ) -> "Runtime":
        settings = settings or load_settings()
        conn = open_database(settings.db_path)
        if github is None:
            token = os.environ.get("MCPM_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN")
            github = GitHubClient(token) if token else None
        bus = events if events is not None else EventBus()
        plugins = PluginService(conn, github=github, events=bus)

        # 阶段 5：安装闭环的装配（受管根目录 + 只读文件来源 + 编排服务）
        roots = ManagedRoots.create(settings.data_dir)

        def provider_factory(plugin) -> GitHubFileProvider:  # type: ignore[no-untyped-def]
            # 来源必须是 github:，且必须固定到具体 commit，否则拒绝安装
            source = require_github_source(plugin.source)
            parts = source.split(":", 1)[1].split("/")
            if len(parts) != 2 or not all(parts):
                raise InstallUnavailable(f"仓库标识非法：{source!r}")
            if github is None:
                raise InstallUnavailable("未配置 GitHub 客户端，无法拉取安装文件")
            if not plugin.pinned_ref:
                raise InstallUnavailable("缺少固定 commit，拒绝安装浮动引用")
            return GitHubFileProvider(github, parts[0], parts[1], plugin.pinned_ref)

        installer = Installer(conn, roots, provider_factory=provider_factory)
        installs = InstallService(
            conn,
            installer,
            plugin_lookup=plugins.get,
            confirmation_ttl=settings.confirmation_ttl_seconds,
        )
        return cls(
            settings=settings,
            conn=conn,
            plugins=plugins,
            events=bus,
            sessions=SessionStore(ttl_seconds=settings.session_ttl_seconds),
            installs=installs,
        )

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:  # pragma: no cover - 关闭失败不影响退出
            pass

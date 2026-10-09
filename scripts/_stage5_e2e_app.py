"""阶段 5 HTTP 端到端验证用的应用工厂（**仅用于本地验证脚本**）。

与 ``app.web.app.create_app`` 的唯一区别：把安装执行器的文件来源换成
内存实现，从而不依赖真实 GitHub 仓库即可验证「会话 → 计划 → 确认 → 执行」
这条授权链路的真实 HTTP 行为。

生产装配（``app.runtime.Runtime.create``）仍然只使用 ``GitHubFileProvider``。
"""

from __future__ import annotations

import os

from app.config import load_settings
from app.install import Installer, InstallService, ManagedRoots
from app.install.provider import MemoryFileProvider
from app.runtime import Runtime
from app.web.app import create_app as _create_app

_FILES = {
    "index.js": b"export const name = 'e2e-demo';\n",
    "package.json": b'{"name":"e2e-demo","version":"1.0.0"}\n',
    "lib/util.js": b"export const add = (a, b) => a + b;\n",
}


def create_app():  # type: ignore[no-untyped-def]
    settings = load_settings()
    runtime = Runtime.create(settings)
    roots = ManagedRoots.create(settings.data_dir)
    installer = Installer(
        runtime.conn,
        roots,
        provider_factory=lambda plugin: MemoryFileProvider(dict(_FILES)),
    )
    runtime.installs = InstallService(
        runtime.conn,
        installer,
        plugin_lookup=runtime.plugins.get,
        confirmation_ttl=settings.confirmation_ttl_seconds,
    )
    os.environ.setdefault("MCPM_E2E", "1")
    return _create_app(runtime)

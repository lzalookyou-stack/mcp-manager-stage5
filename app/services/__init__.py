"""服务层包。

**硬性约束**：网页控制台（``app.web``）与 MCP 工具（``app.mcp_server``）
必须调用本层的同一实现，禁止出现两套业务逻辑。
"""

from __future__ import annotations

from app.install.service import (
    PLAN_TTL_SECONDS,
    InstallService,
    InstallServiceError,
    InstallUnavailable,
)
from app.services.plugin_service import (
    AuditRecord,
    PluginNotFound,
    PluginService,
    SearchUnavailable,
    ValidationError,
    dump_plugin,
)

__all__ = [
    "PluginService",
    "PluginNotFound",
    "ValidationError",
    "SearchUnavailable",
    "AuditRecord",
    "dump_plugin",
    "InstallService",
    "InstallServiceError",
    "InstallUnavailable",
    "PLAN_TTL_SECONDS",
]
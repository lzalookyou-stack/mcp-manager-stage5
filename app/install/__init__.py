"""阶段 5：安全安装闭环。

子模块：
- ``plan``         安装计划模型与摘要
- ``operation``    操作记录与持久化状态机
- ``confirmation`` 确认令牌（绑定计划、一次性、有有效期）
- ``exec``         结构化命令执行（``shell=False``、白名单环境、超时、输出上限）
- ``fsguard``      受管路径、原子写入、快照与归属台账
- ``provider``     固定 commit 下的只读文件来源
- ``installer``    安装 / 回滚执行器（10 步流程）
"""

from __future__ import annotations

from app.install.confirmation import (
    ConfirmationError,
    confirmation_status,
    create_confirmation,
    get_confirmation,
    verify_confirmation,
)
from app.install.exec import CommandRejected, CommandResult, run_command
from app.install.fsguard import (
    FilesystemError,
    ManagedRoots,
    atomic_write,
    capture_snapshot,
    owned_paths,
    record_ownership,
    remove_owned_files,
    restore_snapshot,
)
from app.install.installer import ApplyResult, InstallError, Installer
from app.install.service import (
    PLAN_TTL_SECONDS,
    InstallService,
    InstallServiceError,
    InstallUnavailable,
)
from app.install.operation import (
    Operation,
    OperationError,
    OperationStatus,
    OperationStore,
)
from app.install.plan import (
    DEFAULT_PLAN_TTL_SECONDS,
    InstallPlan,
    PlannedCommand,
    PlannedFile,
    binding_digest,
    build_binding,
    default_expiry,
)
from app.install.provider import (
    FileProvider,
    GitHubFileProvider,
    MemoryFileProvider,
    ProviderError,
    RemoteEntry,
)

__all__ = [
    "InstallPlan",
    "PlannedFile",
    "PlannedCommand",
    "build_binding",
    "binding_digest",
    "default_expiry",
    "DEFAULT_PLAN_TTL_SECONDS",
    "Operation",
    "OperationStore",
    "OperationStatus",
    "OperationError",
    "ConfirmationError",
    "create_confirmation",
    "get_confirmation",
    "verify_confirmation",
    "confirmation_status",
    "run_command",
    "CommandResult",
    "CommandRejected",
    "ManagedRoots",
    "FilesystemError",
    "atomic_write",
    "capture_snapshot",
    "restore_snapshot",
    "record_ownership",
    "owned_paths",
    "remove_owned_files",
    "FileProvider",
    "MemoryFileProvider",
    "GitHubFileProvider",
    "ProviderError",
    "RemoteEntry",
    "Installer",
    "InstallError",
    "ApplyResult",
    "InstallService",
    "InstallServiceError",
    "InstallUnavailable",
]
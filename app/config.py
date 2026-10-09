"""全局配置。

安全默认值（不可被环境变量悄悄放宽到公网）：
- 默认仅监听 ``127.0.0.1``；
- 若显式把 host 设为非回环地址，必须同时设置 ``MCPM_ALLOW_NON_LOOPBACK=1``，否则启动即失败。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:  # pragma: no cover - 配置错误直接失败
        raise ValueError(f"环境变量 {name} 必须是整数，实际为 {raw!r}") from exc


@dataclass(frozen=True)
class Settings:
    """运行期配置快照（不可变）。"""

    project_root: Path
    data_dir: Path
    db_path: Path
    web_dir: Path
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    allow_non_loopback: bool
    max_request_bytes: int

    # --- 阶段 5：安装闭环 ---
    install_enabled: bool
    confirmation_ttl_seconds: int
    session_ttl_seconds: int

    # ------------------------------------------------------------------ #
    @property
    def is_loopback(self) -> bool:
        return self.host in _LOOPBACK_HOSTS

    @property
    def plugins_root(self) -> Path:
        return self.data_dir / "plugins"

    @property
    def snapshots_root(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def config_root(self) -> Path:
        return self.data_dir / "config"

    def validate(self) -> None:
        """启动前自检；不满足安全基线时抛出异常，绝不静默降级。"""
        if not self.is_loopback and not self.allow_non_loopback:
            raise RuntimeError(
                f"拒绝绑定非回环地址 {self.host!r}。"
                "如确需对外暴露，请显式设置 MCPM_ALLOW_NON_LOOPBACK=1 并自行承担风险。"
            )
        if not (1 <= self.port <= 65535):
            raise RuntimeError(f"端口非法：{self.port}")
        # 写操作（安装/回滚）在非回环暴露下必须显式放开，否则拒绝启动。
        if not self.is_loopback and self.install_enabled and not self.allow_non_loopback:
            raise RuntimeError("非回环地址下不允许启用安装写操作")


def load_settings(**overrides: object) -> Settings:
    """加载配置。``overrides`` 用于测试注入，避免污染环境变量。"""
    data_dir = Path(
        overrides.get("data_dir")
        or os.environ.get("MCPM_DATA_DIR")
        or str(PROJECT_ROOT / "var")
    ).expanduser()
    host = str(overrides.get("host") or os.environ.get("MCPM_HOST", "127.0.0.1"))
    port = int(overrides.get("port") or _env_int("MCPM_PORT", 8765))

    allowed_hosts_raw = os.environ.get("MCPM_ALLOWED_HOSTS", "")
    allowed_hosts = tuple(
        h.strip() for h in allowed_hosts_raw.split(",") if h.strip()
    ) or ("127.0.0.1", "localhost")

    allowed_origins_raw = os.environ.get("MCPM_ALLOWED_ORIGINS", "")
    allowed_origins = tuple(
        o.strip() for o in allowed_origins_raw.split(",") if o.strip()
    ) or tuple(f"http://{h}:{port}" for h in allowed_hosts)

    settings = Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        db_path=Path(overrides.get("db_path") or (data_dir / "mcp_manager.db")),
        web_dir=PROJECT_ROOT / "web",
        host=host,
        port=port,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        allow_non_loopback=_env_bool("MCPM_ALLOW_NON_LOOPBACK", False),
        max_request_bytes=_env_int("MCPM_MAX_REQUEST_BYTES", 1_048_576),
        install_enabled=bool(
            overrides.get("install_enabled")
            if overrides.get("install_enabled") is not None
            else _env_bool("MCPM_ENABLE_INSTALL", True)
        ),
        confirmation_ttl_seconds=int(
            overrides.get("confirmation_ttl_seconds")
            or _env_int("MCPM_CONFIRMATION_TTL", 1800)
        ),
        session_ttl_seconds=int(
            overrides.get("session_ttl_seconds")
            or _env_int("MCPM_SESSION_TTL", 3600)
        ),
    )
    settings.validate()
    return settings

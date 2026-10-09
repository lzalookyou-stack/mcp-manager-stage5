"""结构化命令执行（阶段 5）。

安全控制全部是**结构性**的，不依赖正则或提示词：

1. ``argv`` 必须是字符串列表 → 以 ``shell=False`` 交给 ``subprocess``，
   shell 元字符（``;``、``|``、``&&``、``$()``）因此**天然失效**；
2. ``cwd`` 必须落在允许根目录之内（``resolve_within``，防目录穿越与符号链接逃逸）；
3. 环境变量只传白名单键（不把整个 ``os.environ`` 交给子进程）；
4. 有超时；输出有大小上限（超出截断并标注）。

本模块**从不**接受字符串命令，也不提供 ``shell=True`` 的开关。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from app.security import SecurityError, resolve_within

# 传给子进程的环境变量白名单（其余一律不传）
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TZ",
    "TMPDIR",
    "PYTHONPATH",
)

DEFAULT_MAX_OUTPUT_BYTES = 64 * 1024


class CommandRejected(SecurityError):
    """命令在执行前被结构性校验拒绝。调用方**不得**吞掉后继续执行。"""


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    duration_s: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def _validate_argv(argv: object) -> tuple[str, ...]:
    if not isinstance(argv, (list, tuple)) or not argv:
        raise CommandRejected("argv 必须是非空列表（禁止字符串命令）")
    out: list[str] = []
    for item in argv:
        if not isinstance(item, str) or item == "":
            raise CommandRejected(f"argv 的每个元素都必须是非空字符串：{item!r}")
        if "\x00" in item:
            raise CommandRejected("argv 含 NUL 字符")
        out.append(item)
    return tuple(out)


def _resolve_cwd(cwd: Path | str, allowed_roots: list[Path] | tuple[Path, ...]) -> Path:
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.is_absolute():
        raise CommandRejected(f"cwd 必须是绝对路径：{cwd!r}")
    for root in allowed_roots:
        try:
            return resolve_within(Path(root), *cwd_path.relative_to(Path(root)).parts)
        except (SecurityError, ValueError):
            continue
    raise CommandRejected(f"cwd 不在允许的根目录之内：{cwd}")


def _truncate(raw: bytes, limit: int) -> tuple[str, bool]:
    truncated = len(raw) > limit
    if truncated:
        raw = raw[:limit]
    return raw.decode("utf-8", errors="replace"), truncated


def run_command(
    argv: list[str] | tuple[str, ...],
    *,
    cwd: Path | str,
    allowed_roots: list[Path] | tuple[Path, ...],
    timeout_s: float = 30.0,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    """以结构化参数执行一条命令。

    任何一步校验失败都抛 ``CommandRejected``——**不做**"尽力而为"的降级。
    """
    args = _validate_argv(argv)
    if not allowed_roots:
        raise CommandRejected("必须提供 allowed_roots")
    resolved_cwd = _resolve_cwd(cwd, allowed_roots)
    if not resolved_cwd.is_dir():
        raise CommandRejected(f"cwd 不是已存在的目录：{resolved_cwd}")

    timeout_s = float(timeout_s)
    if not (0 < timeout_s <= 600):
        raise CommandRejected(f"timeout_s 超出允许范围：{timeout_s}")

    env = {k: os.environ[k] for k in env_allowlist if k in os.environ}
    if extra_env:
        for key, value in extra_env.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise CommandRejected("extra_env 的键值必须都是字符串")
            env[key] = value

    started = time.monotonic()
    timed_out = False
    try:
        # 关键：shell=False + 列表参数 → 不存在命令注入面
        proc = subprocess.run(  # noqa: S603
            list(args),
            cwd=str(resolved_cwd),
            env=env,
            capture_output=True,
            timeout=timeout_s,
            check=False,
        )
        stdout_raw, stderr_raw = proc.stdout, proc.stderr
        returncode = proc.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_raw = exc.stdout or b""
        stderr_raw = exc.stderr or b""
        returncode = -1
    duration = time.monotonic() - started

    stdout, t1 = _truncate(stdout_raw, max_output_bytes)
    stderr, t2 = _truncate(stderr_raw, max_output_bytes)
    return CommandResult(
        argv=args,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        truncated=t1 or t2,
        timed_out=timed_out,
        duration_s=round(duration, 4),
    )
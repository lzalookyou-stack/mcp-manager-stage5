"""安全原语。

纪律：**不以正则或提示词作为安全控制**。
本模块提供的都是"结构性"控制（路径解析、边界校验、常量时间比较），
正则仅用于**格式快速失败**，不作为唯一防线。
"""

from __future__ import annotations

import hmac
import re
import secrets
from pathlib import Path

# 允许出现在 slug / owner / repo 名里的字符（GitHub 的合法字符集子集）
_IDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
# 40 位 git commit
_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class SecurityError(ValueError):
    """安全校验失败。调用方**不得**吞掉此异常继续执行。"""


def is_safe_identifier(value: str) -> bool:
    """判断是否为安全的标识符（不含 ``/``、``..``、空白、控制字符）。"""
    if not isinstance(value, str):
        return False
    if not _IDENT_RE.fullmatch(value):
        return False
    return value not in {".", ".."}


def require_safe_identifier(value: str, *, what: str = "identifier") -> str:
    if not is_safe_identifier(value):
        raise SecurityError(f"{what} 不合法：{value!r}")
    return value


def is_full_commit(value: str) -> bool:
    return isinstance(value, str) and bool(_COMMIT_RE.fullmatch(value.strip()))


def resolve_within(base: Path, *parts: str) -> Path:
    """把 ``parts`` 拼到 ``base`` 下，并保证结果**确实**位于 ``base`` 之内。

    同时防御：
    - ``..`` 目录穿越
    - 绝对路径注入
    - 符号链接逃逸（对已存在的祖先做 realpath 解析后再次校验）
    """
    base_resolved = Path(base).resolve()
    candidate = base_resolved
    for part in parts:
        if part in ("", "."):
            continue
        if Path(part).is_absolute():
            raise SecurityError(f"拒绝绝对路径分量：{part!r}")
        candidate = candidate / part

    candidate = candidate.resolve()
    if candidate != base_resolved and base_resolved not in candidate.parents:
        raise SecurityError(f"路径逃逸：{candidate} 不在 {base_resolved} 之内")
    return candidate


def constant_time_equals(a: str, b: str) -> bool:
    """常量时间字符串比较，用于令牌 / CSRF token 校验。"""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def new_token(nbytes: int = 32) -> str:
    """生成密码学安全随机令牌。"""
    return secrets.token_urlsafe(nbytes)


def sanitize_for_log(value: object, *, max_len: int = 200) -> str:
    """把任意值压成单行、截断、剔除控制字符，避免日志注入。"""
    text = str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "…"
    return text


# --------------------------------------------------------------------------- #
# 阶段 5：与来源无关的结构性校验
# --------------------------------------------------------------------------- #

# 这些前缀的 URL 不作为安装来源（拒绝非显式来源）
_ALLOWED_SOURCES = ("github:",)


def require_github_source(source: str) -> str:
    """安装来源必须是显式的 ``github:`` 来源，否则拒绝。"""
    if not isinstance(source, str) or not source.startswith(_ALLOWED_SOURCES):
        raise SecurityError(f"安装来源必须是 github: 形式，实际为 {source!r}")
    return source


def reject_shell_metacharacters_ignored() -> None:  # pragma: no cover - 明确留痕
    """**故意留空**：本系统不把 shell 元字符正则当作安全控制。

    真正的控制是「结构化 argv + ``shell=False``」（见 ``app.install.exec``），
    而不是"过滤掉 ``;`` 和 ``|``"这种可被绕过的做法。
    """
    return None
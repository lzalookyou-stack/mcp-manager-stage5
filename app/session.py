"""会话与 CSRF 令牌（阶段 5）。

写操作（安装 / 回滚 / 卸载）必须同时满足：

1. 请求来自**受信任网页**（同源 Origin，由中间件保证）；
2. 携带有效的**会话 Cookie**（``HttpOnly`` + ``SameSite=Strict``）；
3. 携带与该会话绑定的 **CSRF 令牌**（在响应头 ``X-CSRF-Token`` 中下发）。

会话令牌与 CSRF 令牌都只以**哈希**形式保存在内存中，且都有 TTL。
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field

from app.security import constant_time_equals, new_token

SESSION_COOKIE = "mcpm_session"
CSRF_HEADER = "X-CSRF-Token"


class SessionError(RuntimeError):
    """会话 / CSRF 校验失败。调用方**不得**放行。"""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Session:
    token_hash: str
    csrf_hash: str
    created_at: float
    last_seen: float


@dataclass
class SessionStore:
    """进程内会话表（本地单用户场景，够用且不引入额外依赖）。"""

    ttl_seconds: int = 3600
    _sessions: dict[str, Session] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ------------------------------------------------------------------ #
    def create(self) -> tuple[str, str]:
        """创建会话并返回 ``(session_token, csrf_token)``（明文仅此一次）。"""
        session_token = new_token(32)
        csrf_token = new_token(32)
        now = time.time()
        with self._lock:
            self._purge_locked(now)
            self._sessions[_hash(session_token)] = Session(
                token_hash=_hash(session_token),
                csrf_hash=_hash(csrf_token),
                created_at=now,
                last_seen=now,
            )
        return session_token, csrf_token

    def csrf_for(self, session_token: str) -> str | None:
        """返回会话对应的 CSRF 哈希（仅用于比对，不返回明文）。"""
        key = _hash(session_token)
        with self._lock:
            session = self._sessions.get(key)
        return session.csrf_hash if session else None

    def verify(self, session_token: str | None, csrf_token: str | None) -> None:
        """校验会话 + CSRF。任何一项不满足都抛 ``SessionError``。"""
        if not session_token:
            raise SessionError("缺少会话 Cookie，写操作被拒绝")
        if not csrf_token:
            raise SessionError("缺少 CSRF 令牌，写操作被拒绝")
        now = time.time()
        key = _hash(session_token)
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(key)
            if session is None:
                raise SessionError("会话不存在或已过期")
            expected = session.csrf_hash
            session.last_seen = now
        if not constant_time_equals(expected, _hash(csrf_token)):
            raise SessionError("CSRF 令牌不匹配")

    def count(self) -> int:
        with self._lock:
            self._purge_locked(time.time())
            return len(self._sessions)

    # ------------------------------------------------------------------ #
    def _purge_locked(self, now: float) -> None:
        expired = [
            key
            for key, session in self._sessions.items()
            if now - session.last_seen > self.ttl_seconds
        ]
        for key in expired:
            self._sessions.pop(key, None)
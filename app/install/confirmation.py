"""确认令牌（阶段 5）。

**核心安全立场**：Agent 只能提交操作申请；**授权必须来自用户通过受信任网页的明确确认**。
因此：

- 确认令牌由服务端生成（密码学随机），**只把哈希入库**，明文只返回一次；
- 令牌绑定完整的计划绑定字段（见 ``plan.build_binding``）与其摘要；
- 令牌有**有效期**，且**一次性**（用过即作废）；
- 校验使用常量时间比较；
- 计划任何字段变化都会导致摘要不匹配 → 拒绝执行。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db import transaction
from app.install.plan import binding_digest
from app.security import constant_time_equals, new_token


class ConfirmationError(RuntimeError):
    """确认校验失败。调用方**必须**中止操作，绝不降级继续。"""


@dataclass(frozen=True)
class ConfirmationRecord:
    operation_id: str
    token_hash: str
    binding: dict
    binding_digest: str
    expires_at: str
    used_at: str | None
    created_at: str


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def create_confirmation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    binding: dict,
    ttl_seconds: int,
) -> tuple[str, ConfirmationRecord]:
    """生成确认令牌。返回 ``(明文令牌, 记录)``——明文**不会**再被读回。"""
    token = new_token(32)
    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + max(60, int(ttl_seconds))
    record = ConfirmationRecord(
        operation_id=operation_id,
        token_hash=_hash_token(token),
        binding=binding,
        binding_digest=binding_digest(binding),
        expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat(),
        used_at=None,
        created_at=now.isoformat(),
    )
    with transaction(conn):
        conn.execute(
            "INSERT OR REPLACE INTO confirmations(operation_id, token_hash,"
            " binding_json, binding_digest, expires_at, used_at, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (
                record.operation_id,
                record.token_hash,
                json.dumps(record.binding, ensure_ascii=False, sort_keys=True),
                record.binding_digest,
                record.expires_at,
                None,
                record.created_at,
            ),
        )
    return token, record


def get_confirmation(
    conn: sqlite3.Connection, operation_id: str
) -> ConfirmationRecord | None:
    row = conn.execute(
        "SELECT operation_id, token_hash, binding_json, binding_digest, expires_at,"
        " used_at, created_at FROM confirmations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    return ConfirmationRecord(
        operation_id=row["operation_id"],
        token_hash=row["token_hash"],
        binding=json.loads(row["binding_json"]),
        binding_digest=row["binding_digest"],
        expires_at=row["expires_at"],
        used_at=row["used_at"],
        created_at=row["created_at"],
    )


def verify_confirmation(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    token: str,
    current_binding: dict,
    now: datetime | None = None,
    consume: bool = True,
) -> ConfirmationRecord:
    """校验并（默认）消费确认令牌。

    任一条件不满足都抛 ``ConfirmationError``：

    - 令牌不存在 / 已被使用；
    - 令牌哈希不匹配（常量时间比较）；
    - 已过期；
    - 绑定字段摘要与当前计划不一致（计划变了）。
    """
    if not isinstance(token, str) or not token:
        raise ConfirmationError("缺少确认令牌")

    record = get_confirmation(conn, operation_id)
    if record is None:
        raise ConfirmationError("该操作没有确认记录")
    if record.used_at is not None:
        raise ConfirmationError("该确认令牌已被使用")

    now = now or datetime.now(timezone.utc)
    if now >= _parse_iso(record.expires_at):
        raise ConfirmationError("确认令牌已过期，请重新生成计划并重新确认")

    if not constant_time_equals(record.token_hash, _hash_token(token)):
        raise ConfirmationError("确认令牌不匹配")

    if not constant_time_equals(record.binding_digest, binding_digest(current_binding)):
        raise ConfirmationError("计划已变化，原确认失效，必须重新确认")

    if consume:
        with transaction(conn):
            conn.execute(
                "UPDATE confirmations SET used_at = ? WHERE operation_id = ?",
                (now.isoformat(), operation_id),
            )
    return record


def confirmation_status(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, object] | None:
    """只读视图（不含任何令牌信息）。"""
    record = get_confirmation(conn, operation_id)
    if record is None:
        return None
    return {
        "operation_id": record.operation_id,
        "binding_digest": record.binding_digest,
        "expires_at": record.expires_at,
        "used": record.used_at is not None,
        "created_at": record.created_at,
    }
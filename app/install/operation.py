"""操作记录与持久化状态机（阶段 5）。

状态机（见 docs/architecture.md 第 4 节）：

```
pending → awaiting_confirmation → approved → running
                                              ├→ succeeded
                                              ├→ failed → rolling_back → rolled_back
                                              └→ interrupted（进程崩溃后检测）
```

**不变式**：
- 崩溃后未完成的事务被识别为 ``interrupted``，**绝不标记为成功**；
- 每次状态迁移都写操作日志，便于任务历史页面展示真实轨迹。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.db import transaction
from app.install.plan import InstallPlan

VALID_ACTIONS = ("install", "rollback", "uninstall")


class OperationStatus(str, Enum):
    PENDING = "pending"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"
    REJECTED = "rejected"


class OperationError(RuntimeError):
    """操作状态不合法 / 前置条件不满足。"""


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OperationLog:
    seq: int
    ts: str
    step: str
    level: str
    message: str | None


@dataclass(frozen=True)
class Operation:
    id: str
    plugin_id: str
    action: str
    status: str
    actor: str
    plan: InstallPlan | None
    plan_digest: str | None
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "plugin_id": self.plugin_id,
            "action": self.action,
            "status": self.status,
            "actor": self.actor,
            "plan": json.loads(self.plan.model_dump_json()) if self.plan else None,
            "plan_digest": self.plan_digest,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OperationStore:
    """``operations`` 表的读写。线程内串行使用。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ #
    def create(
        self,
        *,
        plugin_id: str,
        action: str,
        actor: str,
        status: OperationStatus,
        plan: InstallPlan | None = None,
        plan_digest: str | None = None,
    ) -> Operation:
        if action not in VALID_ACTIONS:
            raise OperationError(f"未知操作类型：{action!r}")
        if actor not in {"user", "agent", "system"}:
            raise OperationError(f"actor 非法：{actor!r}")
        op_id = uuid.uuid4().hex
        now = _utcnow_iso()
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO operations(id, plugin_id, action, status, actor,"
                " plan_json, plan_digest, binding_json, result_json, error,"
                " created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    op_id,
                    plugin_id,
                    action,
                    status.value,
                    actor,
                    plan.model_dump_json() if plan else None,
                    plan_digest,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
        self.append_log(op_id, step="create", level="info", message=f"{action} 操作已创建")
        return self.get(op_id)

    def get(self, operation_id: str) -> Operation:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise OperationError(f"操作不存在：{operation_id}")
        return self._row_to_operation(row)

    def find(self, operation_id: str) -> Operation | None:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE id = ?", (operation_id,)
        ).fetchone()
        return self._row_to_operation(row) if row else None

    def list(
        self,
        *,
        plugin_id: str | None = None,
        status: OperationStatus | None = None,
        limit: int = 50,
    ) -> list[Operation]:
        clauses: list[str] = []
        params: list[Any] = []
        if plugin_id:
            clauses.append("plugin_id = ?")
            params.append(plugin_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 500))
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT * FROM operations {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_operation(r) for r in rows]

    def latest_succeeded(self, plugin_id: str, *, action: str = "install") -> Operation | None:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE plugin_id = ? AND action = ? AND status = ?"
            " ORDER BY updated_at DESC LIMIT 1",
            (plugin_id, action, OperationStatus.SUCCEEDED.value),
        ).fetchone()
        return self._row_to_operation(row) if row else None

    # ------------------------------------------------------------------ #
    def transition(
        self,
        operation_id: str,
        status: OperationStatus,
        *,
        allowed_from: tuple[OperationStatus, ...] | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        plan: InstallPlan | None = None,
        plan_digest: str | None = None,
        step: str | None = None,
        message: str | None = None,
        level: str = "info",
    ) -> Operation:
        """受控状态迁移。``allowed_from`` 不为空时，当前状态必须在其中。"""
        current = self.get(operation_id)
        if allowed_from is not None:
            allowed = {s.value for s in allowed_from}
            if current.status not in allowed:
                raise OperationError(
                    f"状态不允许该迁移：当前 {current.status}，允许 {sorted(allowed)}"
                )
        now = _utcnow_iso()
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE operations SET status = ?, updated_at = ?,"
                " error = COALESCE(?, error),"
                " result_json = COALESCE(?, result_json),"
                " plan_json = COALESCE(?, plan_json),"
                " plan_digest = COALESCE(?, plan_digest)"
                " WHERE id = ?",
                (
                    status.value,
                    now,
                    error,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    plan.model_dump_json() if plan is not None else None,
                    plan_digest,
                    operation_id,
                ),
            )
        if step:
            self.append_log(operation_id, step=step, level=level, message=message)
        return self.get(operation_id)

    # ------------------------------------------------------------------ #
    def append_log(
        self, operation_id: str, *, step: str, level: str = "info", message: str | None = None
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO operation_logs(operation_id, ts, step, level, message)"
                " VALUES(?,?,?,?,?)",
                (operation_id, _utcnow_iso(), step, level, message),
            )

    def logs(self, operation_id: str, *, limit: int = 200) -> list[OperationLog]:
        rows = self._conn.execute(
            "SELECT seq, ts, step, level, message FROM operation_logs"
            " WHERE operation_id = ? ORDER BY seq ASC LIMIT ?",
            (operation_id, max(1, min(int(limit), 1000))),
        ).fetchall()
        return [OperationLog(**dict(r)) for r in rows]

    def mark_interrupted(self) -> list[str]:
        """把 ``running`` / ``rolling_back`` 的遗留事务标记为 ``interrupted``。

        进程崩溃后调用。**绝不**把它们标记为成功。
        """
        rows = self._conn.execute(
            "SELECT id FROM operations WHERE status IN (?, ?)",
            (OperationStatus.RUNNING.value, OperationStatus.ROLLING_BACK.value),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for op_id in ids:
            self.transition(
                op_id,
                OperationStatus.INTERRUPTED,
                error="进程在操作完成前中断；已标记为 interrupted，未视为成功。",
                step="recover",
                level="warn",
                message="检测到未完成事务，标记为 interrupted",
            )
        return ids

    # ------------------------------------------------------------------ #
    @staticmethod
    def _row_to_operation(row: sqlite3.Row) -> Operation:
        plan = None
        if row["plan_json"]:
            plan = InstallPlan.model_validate_json(row["plan_json"])
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return Operation(
            id=row["id"],
            plugin_id=row["plugin_id"],
            action=row["action"],
            status=row["status"],
            actor=row["actor"],
            plan=plan,
            plan_digest=row["plan_digest"],
            result=result,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
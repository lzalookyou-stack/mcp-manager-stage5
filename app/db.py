"""SQLite 存储层。

- 仅使用标准库 ``sqlite3``（不引入 ORM，见 docs/architecture.md 技术选型）。
- 连接统一开启 ``PRAGMA foreign_keys=ON`` 与 WAL。
- 所有写操作走显式事务；``check_same_thread=False`` 配合调用方串行化。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plugins (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    slug           TEXT NOT NULL,
    name           TEXT NOT NULL,
    kind           TEXT NOT NULL,
    risk_level     TEXT NOT NULL DEFAULT 'none',
    review_status  TEXT NOT NULL DEFAULT 'pending',
    install_status TEXT NOT NULL DEFAULT 'not_installed',
    score_total    REAL NOT NULL DEFAULT 0.0,
    pinned_ref     TEXT,
    data           TEXT NOT NULL,          -- 完整 Plugin 的 JSON 序列化
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    UNIQUE (source, slug)
);

CREATE INDEX IF NOT EXISTS idx_plugins_kind   ON plugins(kind);
CREATE INDEX IF NOT EXISTS idx_plugins_risk   ON plugins(risk_level);
CREATE INDEX IF NOT EXISTS idx_plugins_install ON plugins(install_status);

CREATE TABLE IF NOT EXISTS audit_log (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,              -- 'user' | 'agent' | 'system'
    action     TEXT NOT NULL,
    target     TEXT,
    outcome    TEXT NOT NULL,              -- 'ok' | 'denied' | 'error'
    detail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);

-- ===================== 阶段 5：安全安装闭环 =====================

-- 安装 / 回滚 / 卸载 事务（状态机见 docs/architecture.md 第 4 节）
CREATE TABLE IF NOT EXISTS operations (
    id           TEXT PRIMARY KEY,
    plugin_id    TEXT NOT NULL,
    action       TEXT NOT NULL,          -- install | rollback | uninstall
    status       TEXT NOT NULL,          -- 见 app.install.operation.OperationStatus
    actor        TEXT NOT NULL,          -- user | agent | system
    plan_json    TEXT,                   -- InstallPlan 的 JSON 序列化
    plan_digest  TEXT,                   -- 计划摘要（确认与执行前均重新校验）
    binding_json TEXT,                   -- 确认所绑定的完整字段（含过期时间）
    result_json  TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_operations_plugin ON operations(plugin_id);
CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status);

-- 确认令牌：只存哈希；明文仅在创建时返回一次
CREATE TABLE IF NOT EXISTS confirmations (
    operation_id   TEXT PRIMARY KEY,
    token_hash     TEXT NOT NULL,
    binding_json   TEXT NOT NULL,
    binding_digest TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    used_at        TEXT,
    created_at     TEXT NOT NULL
);

-- 快照：安装前对目标目录的备份（回滚用）
CREATE TABLE IF NOT EXISTS snapshots (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    plugin_id    TEXT NOT NULL,
    path         TEXT NOT NULL,          -- 被快照的绝对路径
    backup_path  TEXT,                   -- 备份位置；NULL 表示原本不存在
    existed      INTEGER NOT NULL,       -- 0/1
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_snapshots_op ON snapshots(operation_id);

-- 文件归属台账：只有这里登记过的文件才允许被本系统删除 / 回滚
CREATE TABLE IF NOT EXISTS file_ownership (
    path         TEXT NOT NULL,
    plugin_id    TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    action       TEXT NOT NULL,          -- create | modify
    sha256       TEXT,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (path, operation_id)
);

CREATE INDEX IF NOT EXISTS idx_ownership_plugin ON file_ownership(plugin_id);

-- 操作步骤日志（供任务历史页面展示真实执行轨迹）
CREATE TABLE IF NOT EXISTS operation_logs (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    ts           TEXT NOT NULL,
    step         TEXT NOT NULL,
    level        TEXT NOT NULL,          -- info | warn | error
    message      TEXT
);

CREATE INDEX IF NOT EXISTS idx_oplogs_op ON operation_logs(operation_id);
"""


def connect(db_path: Path | str) -> sqlite3.Connection:
    """建立连接并完成基础 PRAGMA 设置。"""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """创建 schema（幂等）。"""
    with transaction(conn):
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )


def get_schema_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return int(row["value"]) if row else None


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式事务；异常时回滚并重新抛出（**不吞异常**）。"""
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def open_database(db_path: Path | str) -> sqlite3.Connection:
    """便捷入口：连接 + 初始化。"""
    conn = connect(db_path)
    init_db(conn)
    return conn
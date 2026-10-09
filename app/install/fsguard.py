"""受管文件系统操作（阶段 5）。

原则：
- 所有写入都发生在 ``data_dir`` 之内的**受管根目录**下；
- 每个路径都经过 ``resolve_within`` 规范化 + 越界检查（防 ``..``、绝对路径注入、
  符号链接逃逸）；
- 受管配置用**原子写入**（临时文件 + ``os.replace``）；
- 安装前对目标目录做**快照**；回滚只处理本系统记录并拥有的变更；
- **禁止**递归删除未经本系统登记（``file_ownership``）的目录。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.db import transaction
from app.security import SecurityError, resolve_within


class FilesystemError(RuntimeError):
    """受管文件系统操作失败。"""


@dataclass(frozen=True)
class ManagedRoots:
    """受管根目录集合（全部位于 ``data_dir`` 之下）。"""

    data_dir: Path
    plugins_root: Path
    snapshots_root: Path
    config_root: Path

    @classmethod
    def create(cls, data_dir: Path | str) -> "ManagedRoots":
        base = Path(data_dir).expanduser().resolve()
        roots = cls(
            data_dir=base,
            plugins_root=base / "plugins",
            snapshots_root=base / "snapshots",
            config_root=base / "config",
        )
        for path in (roots.plugins_root, roots.snapshots_root, roots.config_root):
            path.mkdir(parents=True, exist_ok=True)
        return roots

    def plugin_dir(self, plugin_id: str) -> Path:
        """插件安装目录：``plugins/<safe-plugin-id>``。"""
        return resolve_within(self.plugins_root, _safe_component(plugin_id))

    def config_path(self, plugin_id: str) -> Path:
        return resolve_within(self.config_root, f"{_safe_component(plugin_id)}.json")

    def snapshot_dir(self, operation_id: str) -> Path:
        return resolve_within(self.snapshots_root, _safe_component(operation_id))

    def assert_managed(self, path: Path) -> Path:
        """确认 ``path`` 落在某个受管根目录内，否则抛 ``SecurityError``。"""
        resolved = Path(path).resolve()
        for root in (self.plugins_root, self.snapshots_root, self.config_root):
            if resolved == root or root in resolved.parents:
                return resolved
        raise SecurityError(f"路径不在受管根目录内：{resolved}")


def _safe_component(value: str) -> str:
    """把任意标识符压成单层、安全、确定的目录名。"""
    if not isinstance(value, str) or not value.strip():
        raise SecurityError(f"非法标识符：{value!r}")
    cleaned = "".join(
        ch if (ch.isalnum() or ch in "._-") else "-" for ch in value.strip()
    )
    cleaned = cleaned.strip("-.") or "unnamed"
    if cleaned in {".", ".."}:
        raise SecurityError(f"非法标识符：{value!r}")
    return cleaned[:120]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write(path: Path, data: bytes) -> None:
    """原子写入：先写同目录临时文件并 fsync，再 ``os.replace``。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp-{uuid.uuid4().hex[:8]}"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover
                pass


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SnapshotRecord:
    operation_id: str
    plugin_id: str
    path: str
    backup_path: str | None
    existed: bool


def capture_snapshot(
    conn: sqlite3.Connection,
    roots: ManagedRoots,
    *,
    operation_id: str,
    plugin_id: str,
    target: Path,
) -> SnapshotRecord:
    """在修改 ``target`` 之前对它做快照。

    - ``target`` 原本不存在 → 只记录 ``existed=False``（回滚时直接删除我们创建的目录）；
    - ``target`` 原本存在 → 复制到 ``snapshots/<operation_id>/payload``（回滚时还原）。
    """
    target = roots.assert_managed(target)
    backup_dir = roots.snapshot_dir(operation_id)
    existed = target.exists()

    backup_path: Path | None = None
    if existed:
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / "payload"
        if target.is_dir():
            shutil.copytree(target, backup_path, symlinks=False)
        else:
            shutil.copy2(target, backup_path)

    record = SnapshotRecord(
        operation_id=operation_id,
        plugin_id=plugin_id,
        path=str(target),
        backup_path=str(backup_path) if backup_path else None,
        existed=existed,
    )
    with transaction(conn):
        conn.execute(
            "INSERT INTO snapshots(operation_id, plugin_id, path, backup_path,"
            " existed, created_at) VALUES(?,?,?,?,?,?)",
            (
                record.operation_id,
                record.plugin_id,
                record.path,
                record.backup_path,
                1 if record.existed else 0,
                _utcnow_iso(),
            ),
        )
    return record


def restore_snapshot(
    conn: sqlite3.Connection, roots: ManagedRoots, *, operation_id: str
) -> list[SnapshotRecord]:
    """按快照恢复。**只**处理本操作登记的路径。"""
    rows = conn.execute(
        "SELECT operation_id, plugin_id, path, backup_path, existed"
        " FROM snapshots WHERE operation_id = ? ORDER BY seq DESC",
        (operation_id,),
    ).fetchall()
    restored: list[SnapshotRecord] = []
    for row in rows:
        rec = SnapshotRecord(
            operation_id=row["operation_id"],
            plugin_id=row["plugin_id"],
            path=row["path"],
            backup_path=row["backup_path"],
            existed=bool(row["existed"]),
        )
        target = roots.assert_managed(Path(rec.path))
        if target.exists():
            _remove_managed(target)
        if rec.existed and rec.backup_path:
            backup = Path(rec.backup_path)
            if not backup.exists():
                raise FilesystemError(f"快照备份缺失，拒绝声称回滚成功：{backup}")
            target.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_dir():
                shutil.copytree(backup, target, symlinks=False)
            else:
                shutil.copy2(backup, target)
        restored.append(rec)
    return restored


def _remove_managed(path: Path) -> None:
    """删除受管路径。符号链接只删链接本身，绝不跟随。"""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


# --------------------------------------------------------------------------- #
# 归属台账
# --------------------------------------------------------------------------- #


def record_ownership(
    conn: sqlite3.Connection,
    *,
    plugin_id: str,
    operation_id: str,
    files: list[tuple[str, str, str | None]],
) -> None:
    """登记本系统创建 / 修改的文件（``files`` 为 ``(path, action, sha256)``）。"""
    now = _utcnow_iso()
    with transaction(conn):
        conn.executemany(
            "INSERT OR REPLACE INTO file_ownership(path, plugin_id, operation_id,"
            " action, sha256, created_at) VALUES(?,?,?,?,?,?)",
            [
                (path, plugin_id, operation_id, action, sha, now)
                for path, action, sha in files
            ],
        )


def owned_paths(conn: sqlite3.Connection, plugin_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT path FROM file_ownership WHERE plugin_id = ?",
        (plugin_id,),
    ).fetchall()
    return [r["path"] for r in rows]


def remove_owned_files(
    conn: sqlite3.Connection, roots: ManagedRoots, *, plugin_id: str
) -> list[str]:
    """删除归属台账中登记且位于受管根目录内的文件；返回实际删除的路径。"""
    removed: list[str] = []
    for raw in owned_paths(conn, plugin_id):
        try:
            path = roots.assert_managed(Path(raw))
        except SecurityError:
            continue  # 不在受管范围内 → 绝不删除
        if path.exists() or path.is_symlink():
            _remove_managed(path)
            removed.append(str(path))
    return removed
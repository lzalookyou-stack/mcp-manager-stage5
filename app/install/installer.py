"""安装执行器（阶段 5）。

10 步安装流程（见 docs/architecture.md 第 4 节）：

1. 生成并保存安装计划
2. 等待用户**通过受信任网页**确认
3. 验证确认与计划一致
4. 创建快照
5. 暂存目录准备
6. 验证来源、路径、配置
7. 执行安装
8. **原子应用**受管变更
9. 验证结果
10. 记录版本、结果与回滚信息

第 1–3 步在 ``PluginService`` 中编排（涉及确认令牌与审计），
本模块负责第 4–10 步的**文件系统侧**实现。

安全立场：
- 默认**不执行**仓库内的任何安装脚本（``commands`` 为空）；
- 所有写入都在受管根目录内，路径逐级校验；
- 先写暂存目录再 ``os.replace`` 原子替换；
- 每个写入文件都登记归属，回滚只撤销本系统拥有的变更。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.db import transaction
from app.install.fsguard import (
    ManagedRoots,
    atomic_write,
    capture_snapshot,
    record_ownership,
    remove_owned_files,
    restore_snapshot,
)
from app.install.plan import InstallPlan, PlannedCommand, PlannedFile, default_expiry
from app.install.provider import FileProvider, ProviderError
from app.models import Plugin, SecurityReport
from app.security import resolve_within

# 计划有效期（秒）
PLAN_TTL_SECONDS = 1800


class InstallError(RuntimeError):
    """安装 / 回滚执行失败。"""


@dataclass(frozen=True)
class ApplyResult:
    target_dir: str
    config_path: str | None
    written: list[str]
    skipped: list[str]
    commands: list[dict[str, Any]]
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_dir": self.target_dir,
            "config_path": self.config_path,
            "written": self.written,
            "skipped": self.skipped,
            "commands": self.commands,
            "verified": self.verified,
        }


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Installer:
    """受管安装执行器。

    ``provider_factory(plugin) -> FileProvider`` 由上层注入，
    使本模块既能走真实 GitHub，也能在测试中走内存实现。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        roots: ManagedRoots,
        *,
        provider_factory: Callable[[Plugin], FileProvider],
    ) -> None:
        self._conn = conn
        self._roots = roots
        self._provider_factory = provider_factory

    @property
    def roots(self) -> ManagedRoots:
        return self._roots

    # ------------------------------------------------------------------ #
    # 第 1 步：生成计划
    # ------------------------------------------------------------------ #
    def build_plan(self, plugin: Plugin, *, notes: list[str] | None = None) -> InstallPlan:
        if not plugin.pinned_ref:
            raise InstallError(
                "尚未固定到具体 commit，拒绝生成安装计划（浮动引用不可安装）。"
            )
        if plugin.security_report is not None and plugin.security_report.vetoed:
            raise InstallError(
                "安全审查存在 critical 级发现（一票否决），拒绝生成安装计划。"
            )

        provider = self._provider_factory(plugin)
        try:
            entries = provider.list_entries()
        except ProviderError as exc:
            raise InstallError(str(exc)) from exc

        target_dir = self._roots.plugin_dir(plugin.id)
        config_path = self._roots.config_path(plugin.id)

        files = [
            PlannedFile(
                path=e.path,
                action="modify" if (target_dir / e.path).exists() else "create",
                size=e.size,
            )
            for e in entries
        ]

        report = plugin.security_report
        review_summary: dict[str, Any] = {
            "reviewed": report is not None,
            "coverage": report.coverage if report else "none",
            "scanned_files": len(report.scanned_files) if report else 0,
            "skipped_files": len(report.skipped_files) if report else 0,
            "findings": len(report.findings) if report else 0,
            "risk_level": report.risk_level.value if report else "unknown",
            "vetoed": report.vetoed if report else False,
            "pinned_ref": report.pinned_ref if report else None,
            "disclaimer": SecurityReport.DISCLAIMER,
        }

        plan_notes = list(notes or [])
        plan_notes.extend(provider.skipped)
        if report is None:
            plan_notes.append(
                "该条目**尚未**做过安全审查；计划中的风险字段为「未知」，不是「安全」。"
            )
        plan_notes.append(
            "本系统默认**不执行**仓库内的任何安装脚本；安装仅落地文件与受管配置。"
        )

        return InstallPlan(
            plugin_id=plugin.id,
            plugin_name=plugin.name,
            kind=plugin.kind.value,
            source=plugin.source,
            pinned_ref=plugin.pinned_ref,
            target_dir=str(target_dir),
            config_path=str(config_path),
            files=files,
            commands=[],  # 默认不执行任何命令
            permissions=["filesystem:write:managed"],
            runtime_requirements=[
                f"来源固定 commit {plugin.pinned_ref[:12]}…",
                "无需 root；仅写入受管目录",
            ],
            network_required=True,  # 执行时需从来源拉取文件
            spawns_process=False,
            review=review_summary,
            rollback_plan=(
                "回滚将删除本次创建的文件并还原安装前快照；"
                "仅处理本系统登记并拥有的变更。"
            ),
            failure_handling=(
                "任一步失败即中止，进入 failed；已产生的变更通过快照回滚；"
                "进程崩溃后事务标记为 interrupted，绝不视为成功。"
            ),
            notes=plan_notes,
            expires_at=default_expiry(PLAN_TTL_SECONDS),
        ).sealed()

    # ------------------------------------------------------------------ #
    # 第 4–9 步：执行
    # ------------------------------------------------------------------ #
    def apply(self, plugin: Plugin, plan: InstallPlan, *, operation_id: str) -> ApplyResult:
        target_dir = self._roots.assert_managed(Path(plan.target_dir))
        config_path = (
            self._roots.assert_managed(Path(plan.config_path)) if plan.config_path else None
        )

        # 第 4 步：快照（目标目录 + 受管配置）
        capture_snapshot(
            self._conn,
            self._roots,
            operation_id=operation_id,
            plugin_id=plugin.id,
            target=target_dir,
        )
        if config_path is not None:
            capture_snapshot(
                self._conn,
                self._roots,
                operation_id=operation_id,
                plugin_id=plugin.id,
                target=config_path,
            )

        # 第 5 步：暂存目录
        staging = resolve_within(self._roots.plugins_root, f".staging-{operation_id}")
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)

        provider = self._provider_factory(plugin)
        written: list[str] = []
        skipped: list[str] = []
        ownership: list[tuple[str, str, str | None]] = []
        expected: dict[str, str] = {}

        try:
            # 第 6–7 步：拉取并写入暂存区
            for planned in plan.files:
                try:
                    data = provider.read(planned.path)
                except ProviderError as exc:
                    raise InstallError(f"取文件失败：{exc}") from exc
                dest = resolve_within(staging, *Path(planned.path).parts)
                atomic_write(dest, data)
                digest = _sha256(data)
                written.append(planned.path)
                final = resolve_within(target_dir, *Path(planned.path).parts)
                expected[str(final)] = digest
                ownership.append((str(final), "create", digest))

            # 第 8 步：原子应用（目录级 os.replace）
            if target_dir.exists():
                shutil.rmtree(target_dir)
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging, target_dir)

            # 第 9 步：验证结果（逐文件比对 sha256）
            for planned in plan.files:
                final = resolve_within(target_dir, *Path(planned.path).parts)
                if not final.is_file():
                    raise InstallError(f"安装后校验失败：缺少 {planned.path}")
                if _sha256(final.read_bytes()) != expected[str(final)]:
                    raise InstallError(f"安装后校验失败：{planned.path} 内容不一致")

            # 受管配置（原子写入）
            if config_path is not None:
                config_payload = {
                    "plugin_id": plugin.id,
                    "name": plugin.name,
                    "kind": plugin.kind.value,
                    "source": plugin.source,
                    "pinned_ref": plugin.pinned_ref,
                    "installed_by": "mcp-manager",
                    "operation_id": operation_id,
                    "entry": "managed",
                }
                raw = json.dumps(
                    config_payload, ensure_ascii=False, indent=2
                ).encode("utf-8")
                atomic_write(config_path, raw)
                ownership.append((str(config_path), "create", _sha256(raw)))

            # 第 10 步：登记归属
            record_ownership(
                self._conn,
                plugin_id=plugin.id,
                operation_id=operation_id,
                files=ownership,
            )
        except Exception:
            # 失败即回滚已产生的变更，绝不留半成品
            try:
                restore_snapshot(self._conn, self._roots, operation_id=operation_id)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            raise

        return ApplyResult(
            target_dir=str(target_dir),
            config_path=str(config_path) if config_path else None,
            written=written,
            skipped=skipped,
            commands=[],
            verified=True,
        )

    # ------------------------------------------------------------------ #
    # 回滚 / 卸载
    # ------------------------------------------------------------------ #
    def revert(self, *, operation_id: str) -> list[str]:
        """按快照回滚一次安装操作。返回被还原 / 删除的路径。"""
        records = restore_snapshot(self._conn, self._roots, operation_id=operation_id)
        paths = [rec.path for rec in records]
        # 清理该操作登记的归属行（其效果已被快照撤销）
        with transaction(self._conn):
            self._conn.execute(
                "DELETE FROM file_ownership WHERE operation_id = ?", (operation_id,)
            )
        return paths

    def uninstall(self, *, plugin_id: str) -> list[str]:
        """卸载：只删除归属台账中登记且位于受管目录内的文件。"""
        return remove_owned_files(self._conn, self._roots, plugin_id=plugin_id)

    # ------------------------------------------------------------------ #
    def run_planned_commands(
        self, plan: InstallPlan, *, operation_id: str
    ) -> list[dict[str, Any]]:
        """执行计划中的命令。

        当前实现**始终拒绝**——本系统默认禁止自动执行仓库内的安装脚本，
        只有显式放开（未来阶段）才会调用 ``exec.run_command``。
        """
        if not plan.commands:
            return []
        raise InstallError(
            "本系统默认禁止自动执行安装脚本；计划中包含命令，已拒绝执行。"
        )

    @staticmethod
    def planned_command_argv(commands: list[PlannedCommand]) -> list[list[str]]:
        return [list(c.argv) for c in commands]
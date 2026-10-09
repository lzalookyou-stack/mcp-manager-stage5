"""安装计划模型（阶段 5）。

计划是**唯一**被授权执行的东西：用户在受信任网页上确认的，就是这份计划。
因此：

- 计划只包含**结构化**字段（不包含可执行字符串拼接）；
- 计划有**摘要**（``plan_digest``），执行前会重新计算并比对，任何变化立即拒绝；
- 计划有**过期时间**，过期后必须重新生成并重新确认；
- 默认**不包含任何命令**——本系统默认禁止自动执行仓库里的安装脚本。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FileAction = Literal["create", "modify", "delete"]

# 计划默认有效期：超过后确认失效，必须重新生成计划
DEFAULT_PLAN_TTL_SECONDS = 1800


class PlannedFile(BaseModel):
    """计划中的一个文件变更。``path`` 是**相对受管目标目录**的路径。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    action: FileAction = "create"
    size: int = Field(default=0, ge=0)
    sha256: str | None = None


class PlannedCommand(BaseModel):
    """计划中要执行的一条命令。

    **结构化参数**：``argv`` 是列表，调用方必须以 ``shell=False`` 执行，
    绝不允许把列表再拼成字符串交给 shell。
    """

    model_config = ConfigDict(extra="forbid")

    argv: list[str]
    purpose: str
    timeout_s: float = Field(default=30.0, gt=0, le=600)


class InstallPlan(BaseModel):
    """一次安装的完整计划（安装确认页面直接展示本对象）。"""

    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    plugin_name: str
    kind: str
    source: str
    pinned_ref: str

    target_dir: str
    config_path: str | None = None

    files: list[PlannedFile] = Field(default_factory=list)
    commands: list[PlannedCommand] = Field(default_factory=list)

    permissions: list[str] = Field(default_factory=list)
    runtime_requirements: list[str] = Field(default_factory=list)

    network_required: bool = False
    spawns_process: bool = False

    review: dict[str, Any] = Field(
        default_factory=dict,
        description="脚本审查摘要（含覆盖范围、发现数、风险级别与免责声明）",
    )

    rollback_plan: str = ""
    failure_handling: str = ""
    notes: list[str] = Field(default_factory=list)

    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    plan_digest: str = ""

    # ------------------------------------------------------------------ #
    def compute_digest(self) -> str:
        """对**全部安全相关字段**求摘要（顺序无关，避免字典序抖动）。"""
        payload = {
            "plugin_id": self.plugin_id,
            "source": self.source,
            "pinned_ref": self.pinned_ref,
            "kind": self.kind,
            "target_dir": self.target_dir,
            "config_path": self.config_path,
            "files": sorted(
                (f.model_dump() for f in self.files), key=lambda d: d["path"]
            ),
            "commands": [c.model_dump() for c in self.commands],
            "permissions": sorted(self.permissions),
            "network_required": self.network_required,
            "spawns_process": self.spawns_process,
        }
        blob = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def sealed(self) -> "InstallPlan":
        """返回带 ``plan_digest`` 的副本（写入库 / 展示给用户时使用）。"""
        return self.model_copy(update={"plan_digest": self.compute_digest()})

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        now = now or datetime.now(timezone.utc)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return now >= expires

    def files_digest(self) -> str:
        """仅对文件变更清单求摘要（确认绑定字段之一）。"""
        payload = sorted(
            (f.model_dump() for f in self.files), key=lambda d: d["path"]
        )
        blob = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_binding(operation_id: str, plan: InstallPlan) -> dict[str, Any]:
    """确认令牌所绑定的**完整字段集**（见 docs/plan.md 阶段 5 第 2 条）。

    任何一项变化都会导致摘要变化，从而使旧确认失效。
    """
    return {
        "operation_id": operation_id,
        "plugin_id": plan.plugin_id,
        "source": plan.source,
        "pinned_ref": plan.pinned_ref,
        "target_dir": plan.target_dir,
        "config_path": plan.config_path,
        "commands": [list(c.argv) for c in plan.commands],
        "files_digest": plan.files_digest(),
        "files_count": len(plan.files),
        "permissions": sorted(plan.permissions),
        "plan_digest": plan.plan_digest,
        "expires_at": plan.expires_at.isoformat() if plan.expires_at else None,
    }


def binding_digest(binding: dict[str, Any]) -> str:
    blob = json.dumps(
        binding, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def default_expiry(ttl_seconds: int = DEFAULT_PLAN_TTL_SECONDS) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=max(60, int(ttl_seconds)))

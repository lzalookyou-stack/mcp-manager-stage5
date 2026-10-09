"""插件服务层（阶段 3：搜索 / 分析 / 评分 / 安全审查）。

已实现：注册 / 查询 / 列表 / 审计（阶段 2）+ 远程搜索 / 检查 / 评分 / 审查 / 对比（阶段 3）。
写操作（安装 / 回滚 / 卸载）**不在本类实现**：由 ``app.services.install_service.InstallService``
编排（生成计划 → 用户通过受信任网页确认 → 执行 → 登记归属），本类保持纯只读 + 搜索/审查，
任何代码路径都无法绕过用户确认直接写入。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from app.analysis.compare import compare_plugins
from app.analysis.scoring import score_metrics
from app.analysis.security_review import ScannedFile, review_files
from app.db import transaction
from app.models import (
    EvidenceLevel,
    InstallStatus,
    LicenseInfo,
    LicenseSource,
    Plugin,
    PluginKind,
    RepoMetrics,
    ReviewStatus,
    RiskLevel,
    SearchQuery,
)
from app.search.github_client import GitHubClient, GitHubError, RepoInfo
from app.search.query import build_search_query
from app.security import require_safe_identifier

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from app.events import EventBus


class PluginNotFound(LookupError):
    """请求的插件不存在。"""


class ValidationError(ValueError):
    """输入不合法。"""


class SearchUnavailable(RuntimeError):
    """搜索能力不可用（未配置 GitHub 客户端）。

    显式失败，绝不退化为"返回空列表"——空列表会被误读为"没有结果"。
    """


# 安全审查时最多抓取的文件数（控制 API 用量与内存）
_MAX_REVIEW_FILES = 20
# 值得审查的文件后缀（脚本与依赖清单）
_REVIEW_SUFFIXES = (
    ".sh", ".bash", ".zsh", ".ps1", ".psm1", ".bat", ".cmd", ".fish",
    ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl",
)
_REVIEW_FILENAMES = {
    "install", "install.sh", "setup", "setup.sh", "makefile", "dockerfile",
    "package.json", "pyproject.toml", "requirements.txt", "setup.py",
    "setup.cfg", "environment.yml", "gemfile", "cargo.toml", "go.mod",
    "hook.sh", "postinstall.sh", "preinstall.sh",
}
_REVIEW_DIRS = ("", "scripts", "install", "hooks", "bin", ".github/workflows")


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    ts: str
    actor: str
    action: str
    target: str | None
    outcome: str
    detail: str | None


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PluginService:
    """统一业务入口。线程内串行使用（SQLite 单连接）。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        github: GitHubClient | None = None,
        events: "EventBus | None" = None,
    ) -> None:
        self._conn = conn
        self._github = github
        self._events = events

    # ------------------------------------------------------------------ #
    # 事件（阶段 4：供 SSE 推送；失败绝不影响主流程）
    # ------------------------------------------------------------------ #
    def emit(self, event_type: str, **data: Any) -> None:
        """向事件总线发布一条事件；未配置总线时静默忽略。"""
        if self._events is None:
            return
        self._events.publish(event_type, data)

    # ------------------------------------------------------------------ #
    # 审计
    # ------------------------------------------------------------------ #
    def audit(
        self,
        *,
        actor: str,
        action: str,
        target: str | None = None,
        outcome: str = "ok",
        detail: str | None = None,
    ) -> None:
        """记录一条审计日志。``actor`` 只允许 user / agent / system。"""
        if actor not in {"user", "agent", "system"}:
            raise ValidationError(f"actor 非法：{actor!r}")
        if outcome not in {"ok", "denied", "error"}:
            raise ValidationError(f"outcome 非法：{outcome!r}")
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO audit_log(ts, actor, action, target, outcome, detail)"
                " VALUES(?,?,?,?,?,?)",
                (_utcnow_iso(), actor, action, target, outcome, detail),
            )
        # 审计写入成功后广播（detail 已在调用侧脱敏；此处只发结构化字段）。
        self.emit(
            "audit",
            actor=actor,
            action=action,
            target=target,
            outcome=outcome,
            detail=detail,
        )

    def list_audit(self, limit: int = 50) -> list[AuditRecord]:
        limit = max(1, min(int(limit), 500))
        rows = self._conn.execute(
            "SELECT seq, ts, actor, action, target, outcome, detail"
            " FROM audit_log ORDER BY seq DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [AuditRecord(**dict(r)) for r in rows]

    # ------------------------------------------------------------------ #
    # 写入
    # ------------------------------------------------------------------ #
    def upsert(self, plugin: Plugin, *, actor: str = "system") -> Plugin:
        """插入或更新插件（按稳定 ID 幂等）。"""
        if not isinstance(plugin, Plugin):
            raise ValidationError("plugin 必须是 Plugin 实例")

        now = _utcnow_iso()
        payload = plugin.model_dump_json()
        with transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO plugins(id, source, slug, name, kind, risk_level,
                                    review_status, install_status, score_total,
                                    pinned_ref, data, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    kind=excluded.kind,
                    risk_level=excluded.risk_level,
                    review_status=excluded.review_status,
                    install_status=excluded.install_status,
                    score_total=excluded.score_total,
                    pinned_ref=excluded.pinned_ref,
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (
                    plugin.id,
                    plugin.source,
                    plugin.slug,
                    plugin.name,
                    plugin.kind.value,
                    plugin.risk_level.value,
                    plugin.review_status.value,
                    plugin.install_status.value,
                    plugin.score.total(),
                    plugin.pinned_ref,
                    payload,
                    plugin.created_at.isoformat(),
                    now,
                ),
            )
        self.audit(actor=actor, action="plugin.upsert", target=plugin.id, outcome="ok")
        return plugin

    def register_placeholder(
        self,
        *,
        source: str,
        slug: str,
        name: str,
        kind: PluginKind,
        actor: str = "user",
        **kwargs: Any,
    ) -> Plugin:
        """注册一个尚未经过完整审查的条目。

        **注意**：此类条目的 ``review_status`` 为 pending、``risk_level`` 为 none，
        含义是"尚未评估"，而不是"已确认安全"。调用方不得将其视为已审查。
        """
        require_safe_identifier(source.split(":")[-1].replace("/", "-"), what="source")
        require_safe_identifier(slug, what="slug")
        plugin = Plugin.new(source=source, slug=slug, name=name, kind=kind, **kwargs)
        return self.upsert(plugin, actor=actor)

    # ------------------------------------------------------------------ #
    # 读取
    # ------------------------------------------------------------------ #
    def get(self, plugin_id: str) -> Plugin:
        row = self._conn.execute(
            "SELECT data FROM plugins WHERE id = ?", (plugin_id,)
        ).fetchone()
        if row is None:
            raise PluginNotFound(plugin_id)
        return Plugin.model_validate_json(row["data"])

    def list(
        self,
        *,
        kind: PluginKind | None = None,
        risk_level: RiskLevel | None = None,
        install_status: InstallStatus | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Plugin]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if risk_level is not None:
            clauses.append("risk_level = ?")
            params.append(risk_level.value)
        if install_status is not None:
            clauses.append("install_status = ?")
            params.append(install_status.value)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        params.extend([limit, offset])

        rows = self._conn.execute(
            f"SELECT data FROM plugins {where}"
            " ORDER BY score_total DESC, name ASC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [Plugin.model_validate_json(r["data"]) for r in rows]

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) AS c FROM plugins").fetchone()["c"])

    def stats(self) -> dict[str, Any]:
        by_kind = {
            r["kind"]: r["c"]
            for r in self._conn.execute(
                "SELECT kind, COUNT(*) AS c FROM plugins GROUP BY kind"
            )
        }
        by_install = {
            r["install_status"]: r["c"]
            for r in self._conn.execute(
                "SELECT install_status, COUNT(*) AS c FROM plugins GROUP BY install_status"
            )
        }
        return {
            "total": self.count(),
            "by_kind": by_kind,
            "by_install_status": by_install,
        }

    # ------------------------------------------------------------------ #
    # 阶段 3：远程搜索 / 分析 / 评分 / 安全审查 / 对比
    # ------------------------------------------------------------------ #
    @property
    def github(self) -> GitHubClient:
        if self._github is None:
            raise SearchUnavailable(
                "未配置 GitHub 客户端，远程搜索不可用。"
                "请设置 MCPM_GITHUB_TOKEN 后重试（匿名调用易被限流）。"
            )
        return self._github

    def build_query(self, requirement: str, **filters: Any) -> SearchQuery:
        """把自然语言需求转成 GitHub 检索式（不发起网络请求）。"""
        if not isinstance(requirement, str) or not requirement.strip():
            raise ValidationError("requirement 不能为空")
        try:
            return build_search_query(requirement, **filters)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def search_remote(
        self,
        query: str,
        *,
        limit: int = 10,
        actor: str = "user",
    ) -> list[Plugin]:
        """在 GitHub 上搜索仓库并落库为候选条目。

        - ``query`` 可以是自然语言需求，也可以是现成的 GitHub 检索式；
        - **网络/限流/权限错误会向上抛出** ``GitHubError``，不返回伪造结果；
        - 结果条目的 ``review_status`` 为 ``pending``、``risk_level`` 为 ``none``，
          含义是"尚未评估"，**不是**"已确认安全"。
        """
        if not isinstance(query, str) or not query.strip():
            raise ValidationError("query 不能为空")
        limit = max(1, min(int(limit), 50))

        client = self.github
        # 若看起来已含 GitHub 限定符（含 ':'），按原样使用；否则先做需求转换
        if ":" in query and " " not in query.split(":", 1)[0]:
            search_expr = query.strip()
            parsed: SearchQuery | None = None
        else:
            parsed = self.build_query(query)
            search_expr = parsed.github_query

        self.emit("search.started", query=search_expr, limit=limit)
        try:
            infos = client.search_repositories(search_expr, per_page=limit)
        except GitHubError as exc:
            self.emit(
                "search.failed", query=search_expr, kind=exc.kind, error=str(exc)
            )
            raise
        plugins: list[Plugin] = []
        for info in infos:
            plugins.append(self._upsert_repo(info, actor=actor))
        self.audit(
            actor=actor,
            action="search.remote",
            target=search_expr,
            outcome="ok",
            detail=f"命中 {len(plugins)} 个候选",
        )
        self.emit("search.finished", query=search_expr, count=len(plugins))
        return plugins

    def _upsert_repo(self, info: RepoInfo, *, actor: str) -> Plugin:
        """把一次 GitHub 仓库查询结果落库为候选条目。"""
        metrics = self._metrics_from(info)
        score = score_metrics(metrics, security_report=None)
        plugin = Plugin.new(
            source=f"github:{info.full_name}",
            slug=info.name or info.full_name.split("/")[-1],
            name=info.full_name,
            kind=PluginKind.MCP_SERVER,
            description=info.description,
            repository=info.html_url or None,
            license=LicenseInfo(
                spdx_id=info.license_spdx,
                name=info.license_name,
                source=(
                    LicenseSource.API_FIELD
                    if info.license_spdx
                    else LicenseSource.UNKNOWN
                ),
                evidence=(
                    EvidenceLevel.VERIFIED
                    if info.license_spdx
                    else EvidenceLevel.UNCHECKED
                ),
            ),
            risk_level=RiskLevel.NONE,
            score=score,
            review_status=ReviewStatus.PENDING,
            stars=info.stars,
            tags=list(info.topics),
            fetched_at=info.fetched_at,
            missing_fields=list(info.missing_fields),
            metadata={
                "default_branch": info.default_branch,
                "open_issues": info.open_issues,
                "forks": info.forks,
                "language": info.language,
                "archived": info.archived,
                "pushed_at": info.pushed_at.isoformat() if info.pushed_at else None,
                "size_kb": info.size_kb,
            },
        )
        return self.upsert(plugin, actor=actor)

    @staticmethod
    def _metrics_from(info: RepoInfo) -> RepoMetrics:
        return RepoMetrics(
            full_name=info.full_name,
            stars=info.stars,
            forks=info.forks,
            open_issues=info.open_issues,
            pushed_at=info.pushed_at,
            created_at=info.created_at,
            archived=info.archived,
            language=info.language,
            license_spdx=info.license_spdx,
            license_source=(
                LicenseSource.API_FIELD if info.license_spdx else LicenseSource.UNKNOWN
            ),
            fetched_at=info.fetched_at,
            missing_fields=list(info.missing_fields),
        )

    def inspect_remote(
        self,
        plugin_id: str,
        *,
        actor: str = "user",
    ) -> Plugin:
        """补全单个候选的深度信息：固定 commit、README/测试/CI/依赖清单探测、Release 数。

        任何子项获取失败都会记入 ``missing_fields`` 并**如实保留**，
        不会用默认值假装成功。
        """
        plugin = self.get(plugin_id)
        # 先做本地可判定的输入校验，再要求远程客户端可用：
        # 否则"这不是 GitHub 源"会被误报成"未配置客户端"，掩盖真正的根因。
        owner, _, repo = self._split_repository(plugin)
        client = self.github

        missing: list[str] = []
        metrics = RepoMetrics(
            full_name=f"{owner}/{repo}",
            stars=plugin.stars,
            license_spdx=plugin.license.spdx_id,
            license_source=plugin.license.source,
        )
        metadata = dict(plugin.metadata)
        pinned_ref = plugin.pinned_ref

        try:
            info = client.get_repository(owner, repo)
            metrics = self._metrics_from(info)
        except GitHubError as exc:
            missing.append(f"repository:{exc.kind}")

        try:
            pinned_ref = client.get_latest_commit(owner, repo)
            metadata["latest_commit"] = pinned_ref
        except GitHubError as exc:
            missing.append(f"commit:{exc.kind}")

        for probe, field_name in (
            (lambda: client.list_directory(owner, repo, ""), "root"),
        ):
            try:
                root_entries = probe()
                names = {n.lower() for n in root_entries}
                metrics = metrics.model_copy(
                    update={
                        "has_readme": any(n.startswith("readme") for n in names),
                        "has_dependency_manifest": bool(
                            names
                            & {
                                "requirements.txt", "pyproject.toml", "package.json",
                                "cargo.toml", "go.mod", "gemfile", "setup.py",
                            }
                        ),
                        "has_tests": bool(
                            names & {"tests", "test", "spec", "__tests__"}
                        ) or any(n.startswith("test_") for n in names),
                    }
                )
                metadata["root_entries"] = sorted(root_entries)[:50]
            except GitHubError as exc:
                missing.append(f"{field_name}:{exc.kind}")

        try:
            workflows = client.list_directory(owner, repo, ".github/workflows")
            metrics = metrics.model_copy(update={"has_ci": bool(workflows)})
        except GitHubError as exc:
            missing.append(f"ci:{exc.kind}")

        try:
            releases = client.list_releases(owner, repo, per_page=5)
            metrics = metrics.model_copy(
                update={
                    "releases_count": len(releases),
                    "license_source": metrics.license_source,
                }
            )
            if releases:
                metadata["latest_release"] = releases[0].get("tag_name")
        except GitHubError as exc:
            missing.append(f"releases:{exc.kind}")

        # 保持评分与最新指标一致；安全维度仍未审查 → 0 分（保守）
        score = score_metrics(
            metrics, security_report=plugin.security_report
        )

        updated = plugin.model_copy(
            update={
                "pinned_ref": pinned_ref,
                "score": score,
                "stars": metrics.stars,
                "fetched_at": datetime.now(timezone.utc),
                "missing_fields": sorted(set(plugin.missing_fields) | set(missing)),
                "metadata": metadata,
            }
        )
        return self.upsert(updated, actor=actor)

    @staticmethod
    def _split_repository(plugin: Plugin) -> tuple[str, str, str]:
        source = plugin.source or ""
        if not source.startswith("github:"):
            raise ValidationError(f"仅支持 github: 来源，实际为 {source!r}")
        full = source.split(":", 1)[1]
        parts = full.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValidationError(f"仓库标识非法：{full!r}")
        owner, repo = parts
        require_safe_identifier(owner, what="owner")
        require_safe_identifier(repo, what="repo")
        return owner, "/", repo

    def review(
        self,
        plugin_id: str,
        *,
        actor: str = "user",
        max_files: int = _MAX_REVIEW_FILES,
    ) -> Plugin:
        """对候选做静态安全审查，并把结果写入条目。

        - 审查针对**固定 commit**（若尚无 ``pinned_ref`` 会先尝试补全）；
        - 抓取失败的文件记入 ``skipped_files``，**不**假装已扫描；
        - 报告**不含**任何「绝对安全」结论，只陈述覆盖范围与命中项。
        """
        plugin = self.get(plugin_id)
        # 同 inspect_remote：先做本地可判定的输入校验，再要求客户端可用。
        owner, _, repo = self._split_repository(plugin)
        client = self.github

        if not plugin.pinned_ref:
            plugin = self.inspect_remote(plugin_id, actor=actor)
            if not plugin.pinned_ref:
                raise ValidationError(
                    "无法确定固定 commit，拒绝在浮动引用上给出审查结论。"
                )

        owner, _, repo = self._split_repository(plugin)
        ref = plugin.pinned_ref
        assert ref is not None

        candidates = self._collect_review_paths(client, owner, repo, ref, max_files)
        self.emit(
            "review.started",
            plugin_id=plugin_id,
            ref=ref,
            files=len(candidates),
        )
        scanned: list[ScannedFile] = []
        skipped: list[str] = []
        notes: list[str] = []
        for index, path in enumerate(candidates, start=1):
            try:
                file = client.get_file(owner, repo, path, ref=ref)
            except GitHubError as exc:
                skipped.append(f"{path}（{exc.kind}）")
                self.emit(
                    "review.progress",
                    plugin_id=plugin_id,
                    index=index,
                    total=len(candidates),
                    path=path,
                    outcome="skipped",
                    error=exc.kind,
                )
                continue
            if file.truncated:
                notes.append(f"{path} 超过大小上限已截断，可能遗漏后段内容。")
            scanned.append(ScannedFile(path=path, content=file.content))
            self.emit(
                "review.progress",
                plugin_id=plugin_id,
                index=index,
                total=len(candidates),
                path=path,
                outcome="scanned",
            )

        report = review_files(
            scanned,
            coverage="partial" if candidates else "none",
            pinned_ref=ref,
            notes=notes,
        )
        report = report.model_copy(
            update={"skipped_files": sorted(set(report.skipped_files) | set(skipped))}
        )

        score = score_metrics(self._metrics_from_plugin(plugin), security_report=report)
        review_status = (
            ReviewStatus.REJECTED if report.vetoed else ReviewStatus.REVIEWING
        )
        updated = plugin.model_copy(
            update={
                "security_report": report,
                "risk_level": report.risk_level,
                "score": score,
                "review_status": review_status,
            }
        )
        result = self.upsert(updated, actor=actor)
        self.audit(
            actor=actor,
            action="plugin.review",
            target=plugin_id,
            outcome="ok",
            detail=(
                f"覆盖 {len(report.scanned_files)} 文件，"
                f"{len(report.findings)} 条发现，风险 {report.risk_level.value}"
            ),
        )
        self.emit(
            "review.finished",
            plugin_id=plugin_id,
            ref=ref,
            scanned=len(report.scanned_files),
            findings=len(report.findings),
            risk=report.risk_level.value,
            vetoed=report.vetoed,
        )
        return result

    def _collect_review_paths(
        self,
        client: GitHubClient,
        owner: str,
        repo: str,
        ref: str,
        max_files: int,
    ) -> list[str]:
        """在若干候选目录里挑选值得审查的文件（受 ``max_files`` 限制）。"""
        found: list[str] = []
        seen: set[str] = set()
        for directory in _REVIEW_DIRS:
            try:
                entries = client.list_directory(owner, repo, directory, ref=ref)
            except GitHubError:
                continue
            for name in entries:
                lower = name.lower()
                if directory:
                    path = f"{directory}/{name}"
                else:
                    path = name
                if path in seen:
                    continue
                if lower in _REVIEW_FILENAMES or lower.endswith(_REVIEW_SUFFIXES):
                    seen.add(path)
                    found.append(path)
                if len(found) >= max_files:
                    return found
        return found

    @staticmethod
    def _metrics_from_plugin(plugin: Plugin) -> RepoMetrics:
        meta = plugin.metadata or {}
        pushed = meta.get("pushed_at")
        pushed_at = None
        if isinstance(pushed, str):
            try:
                pushed_at = datetime.fromisoformat(pushed)
            except ValueError:
                pushed_at = None
        return RepoMetrics(
            full_name=plugin.source.split(":", 1)[-1],
            stars=plugin.stars,
            forks=int(meta.get("forks") or 0),
            open_issues=int(meta.get("open_issues") or 0),
            pushed_at=pushed_at,
            archived=bool(meta.get("archived")),
            language=meta.get("language"),
            license_spdx=plugin.license.spdx_id,
            license_source=plugin.license.source,
            fetched_at=plugin.fetched_at,
            missing_fields=list(plugin.missing_fields),
        )

    def score(self, plugin_id: str, *, actor: str = "user") -> Plugin:
        """依据当前指标与安全报告重算可解释评分。"""
        plugin = self.get(plugin_id)
        score = score_metrics(
            self._metrics_from_plugin(plugin),
            security_report=plugin.security_report,
        )
        updated = plugin.model_copy(update={"score": score})
        return self.upsert(updated, actor=actor)

    def compare(self, plugin_ids: list[str]) -> dict[str, object]:
        """对比多个候选（只呈现已核实字段与证据缺口）。"""
        if not plugin_ids:
            raise ValidationError("plugin_ids 不能为空")
        plugins = [self.get(pid) for pid in plugin_ids]
        return compare_plugins(plugins)

    # ------------------------------------------------------------------ #
    # 写操作（安装 / 回滚 / 卸载）
    # ------------------------------------------------------------------ #
    # 刻意**不**在本类提供 install / rollback / uninstall：
    # 写操作必须经 ``app.services.install_service.InstallService`` 的
    # 「生成计划 → 用户通过受信任网页确认 → 校验令牌 → 执行」流程，
    # 从而保证没有任何代码路径可以绕过用户授权直接写入文件系统。


def dump_plugin(plugin: Plugin) -> dict[str, Any]:
    """稳定的 JSON 视图（供 API / MCP 复用）。"""
    return json.loads(plugin.model_dump_json())
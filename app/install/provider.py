"""安装来源抽象（阶段 5）。

安装执行时需要把**固定 commit** 下的文件取到本地受管目录。
本模块只定义"取文件"的接口与一个 GitHub 实现；测试可注入内存实现，
从而在不触网的前提下验证完整的安装 / 回滚闭环。

注意：``FileProvider`` **只读**，且不执行任何仓库内的代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from app.search.github_client import GitHubClient, GitHubError

# 单个文件大小上限（超过即跳过并如实记录，不静默忽略）
MAX_INSTALL_FILE_BYTES = 512 * 1024
# 单次安装最多取多少个文件
MAX_INSTALL_FILES = 300


class ProviderError(RuntimeError):
    """取文件失败。"""


@dataclass(frozen=True)
class RemoteEntry:
    path: str
    size: int


class FileProvider(Protocol):
    """固定 commit 下的只读文件视图。"""

    @property
    def skipped(self) -> list[str]:
        """被跳过的文件及原因（如实暴露覆盖缺口）。"""
        ...

    def list_entries(self) -> list[RemoteEntry]: ...

    def read(self, path: str) -> bytes: ...


class MemoryFileProvider:
    """内存实现（测试 / 本地目录导入用）。"""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = dict(files)
        self._skipped: list[str] = []

    @property
    def skipped(self) -> list[str]:
        return list(self._skipped)

    def list_entries(self) -> list[RemoteEntry]:
        self._skipped = []
        entries: list[RemoteEntry] = []
        for path, data in sorted(self._files.items()):
            if len(data) > MAX_INSTALL_FILE_BYTES:
                self._skipped.append(f"{path}（超过 {MAX_INSTALL_FILE_BYTES} 字节上限）")
                continue
            entries.append(RemoteEntry(path=path, size=len(data)))
        return entries[:MAX_INSTALL_FILES]

    def read(self, path: str) -> bytes:
        if path not in self._files:
            raise ProviderError(f"文件不存在：{path}")
        return self._files[path]


class GitHubFileProvider:
    """基于 GitHub git-tree API 的只读实现。"""

    def __init__(
        self,
        client: GitHubClient,
        owner: str,
        repo: str,
        ref: str,
        *,
        max_files: int = MAX_INSTALL_FILES,
        max_file_bytes: int = MAX_INSTALL_FILE_BYTES,
    ) -> None:
        self._client = client
        self._owner = owner
        self._repo = repo
        self._ref = ref
        self._max_files = max_files
        self._max_file_bytes = max_file_bytes
        self._skipped: list[str] = []

    @property
    def skipped(self) -> list[str]:
        return list(self._skipped)

    def list_entries(self) -> list[RemoteEntry]:
        self._skipped = []
        try:
            tree = self._client.list_tree(self._owner, self._repo, self._ref)
        except GitHubError as exc:
            raise ProviderError(f"无法读取文件清单（{exc.kind}）：{exc}") from exc

        entries: list[RemoteEntry] = []
        for item in tree:
            if item.get("type") != "blob":
                continue
            path = item.get("path")
            if not isinstance(path, str) or not path:
                continue
            size = int(item.get("size") or 0)
            if size > self._max_file_bytes:
                self._skipped.append(
                    f"{path}（{size} 字节，超过 {self._max_file_bytes} 字节上限）"
                )
                continue
            entries.append(RemoteEntry(path=path, size=size))
            if len(entries) >= self._max_files:
                self._skipped.append(f"其余文件因超过 {self._max_files} 个上限未纳入计划")
                break
        return entries

    def read(self, path: str) -> bytes:
        try:
            file = self._client.get_file(self._owner, self._repo, path, ref=self._ref)
        except GitHubError as exc:
            raise ProviderError(f"读取失败（{exc.kind}）：{path}") from exc
        if file.truncated:
            raise ProviderError(f"文件被截断，拒绝写入不完整内容：{path}")
        return file.content.encode("utf-8")


def provider_summary(provider: FileProvider) -> dict[str, Any]:
    """把 provider 的覆盖缺口压成可展示的结构。"""
    return {"skipped": list(provider.skipped)}
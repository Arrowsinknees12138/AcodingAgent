"""Repository Service 的协议与错误类型（实施设计第 12.2、20 节）。

具体实现（真正调用 git、读写 Artifact Store）在
`infrastructure/git/repository_service.py`；这里只放接口和跟
`ErrorCode` 对应的异常类型，方便上层 Activity 捕获后转换成
`ErrorInfo`，不需要 import 具体的 git 实现。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.plans import CandidateSource, WorkItem
from repopilot.domain.tasks import TaskSpec


class UnsupportedRepositoryError(RuntimeError):
    """对应 `ErrorCode.UNSUPPORTED_REPOSITORY`：仓库/revision 超出 Phase 0 支持范围。"""


class RevisionNotFoundError(RuntimeError):
    """对应 `ErrorCode.REVISION_NOT_FOUND`。"""


class PatchPathDeniedError(RuntimeError):
    """对应 `ErrorCode.PATCH_PATH_DENIED`：patch 修改了 allowlist 之外的路径。"""

    def __init__(self, denied_paths: tuple[str, ...]) -> None:
        super().__init__(f"patch 修改了未授权路径: {', '.join(denied_paths)}")
        self.denied_paths = denied_paths


class DependencyChangeDeniedError(RuntimeError):
    """默认禁止 patch 修改 Python 依赖清单，除非任务显式授权。"""

    def __init__(self, manifest_paths: tuple[str, ...]) -> None:
        super().__init__(f"任务未授权依赖变更: {', '.join(manifest_paths)}")
        self.manifest_paths = manifest_paths


class PatchApplyFailedError(RuntimeError):
    """对应 `ErrorCode.PATCH_APPLY_FAILED`：`git apply --check` 未通过、AST 解析失败等。"""


class PatchConflictError(RuntimeError):
    """对应 `ErrorCode.PATCH_CONFLICT`：并行分支集成时发生冲突，需人工/Replan 处理。"""


class RepositoryService(Protocol):
    async def resolve_revision(self, repo_url: str, revision: str | None) -> str: ...

    async def snapshot(self, task: TaskSpec) -> ArtifactRef: ...

    async def create_developer_context(
        self,
        work_item: WorkItem,
        integrated_patches: tuple[ArtifactRef, ...],
    ) -> ArtifactRef: ...

    # 实施设计 12.2 节把 `integrate_patch` 写成只接受 `proposal_ref` 一个
    # 参数，但 12.4 节第 4 步明确要求"校验 touched paths 与 WorkItem
    # allowlist 完全一致"——而 WorkItem 本身在这套设计里没有独立的
    # ArtifactKind/存储位置可供 RepositoryService 反查。这里显式加一个
    # `allowed_write_paths` 参数，由调用方（持有 WorkItem 的 Activity）
    # 直接传入，而不是发明一套 WorkItem 持久化机制去满足一个只有单参数的
    # 接口签名。
    async def integrate_patch(
        self,
        proposal_ref: ArtifactRef,
        *,
        allowed_write_paths: tuple[str, ...],
        allow_dependency_changes: bool = False,
    ) -> ArtifactRef: ...

    async def final_diff(self, run_id: UUID) -> ArtifactRef: ...

    async def export_candidate(self, run_id: UUID) -> CandidateSource: ...

    async def cleanup(self, run_id: UUID) -> ArtifactRef: ...

"""Repository Service 的 Temporal Activity 适配层。"""

from __future__ import annotations

from temporalio import activity

from repopilot.domain.artifacts import ArtifactCaller, ArtifactRef
from repopilot.domain.tasks import TaskSpec
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.repository_service import RepositoryService


class RepositoryActivities:
    def __init__(self, *, artifact_store: ArtifactStore, repository: RepositoryService) -> None:
        self._artifacts = artifact_store
        self._repository = repository

    @activity.defn(name="scan_repository")
    async def scan_repository(self, task_spec_ref: ArtifactRef) -> ArtifactRef:
        caller = ArtifactCaller(
            tenant_id=task_spec_ref.tenant_id,
            run_id=task_spec_ref.run_id,
            role=None,
            service="repository",
        )
        task = TaskSpec.model_validate_json(await self._artifacts.get_bytes(task_spec_ref, caller))
        return await self._repository.snapshot(task)

"""Repository Service 的 Temporal Activity 适配层。"""

from __future__ import annotations

from uuid import UUID

from temporalio import activity

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactCaller, ArtifactRef
from repopilot.domain.plans import CandidateSource, WorkItem
from repopilot.domain.tasks import TaskSpec
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.repository_service import RepositoryService


class BuildDeveloperContextInput(StrictModel):
    work_item: WorkItem
    task_spec_ref: ArtifactRef
    integrated_patch_refs: tuple[ArtifactRef, ...]


class IntegratePatchInput(StrictModel):
    proposal_ref: ArtifactRef
    work_item: WorkItem
    task_spec_ref: ArtifactRef


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

    @activity.defn(name="build_developer_context")
    async def build_developer_context(self, payload: BuildDeveloperContextInput) -> ArtifactRef:
        return await self._repository.create_developer_context(
            payload.work_item,
            payload.integrated_patch_refs,
        )

    @activity.defn(name="integrate_patch")
    async def integrate_patch(self, payload: IntegratePatchInput) -> ArtifactRef:
        task_ref = payload.task_spec_ref
        caller = ArtifactCaller(
            tenant_id=task_ref.tenant_id,
            run_id=task_ref.run_id,
            role=None,
            service="repository",
        )
        task = TaskSpec.model_validate_json(await self._artifacts.get_bytes(task_ref, caller))
        return await self._repository.integrate_patch(
            payload.proposal_ref,
            allowed_write_paths=payload.work_item.allowed_write_paths,
            allow_dependency_changes=task.dependency_policy.allow_new_dependencies,
        )

    @activity.defn(name="export_candidate")
    async def export_candidate(self, run_id: UUID) -> CandidateSource:
        return await self._repository.export_candidate(run_id)

    @activity.defn(name="build_final_diff")
    async def build_final_diff(self, run_id: UUID) -> ArtifactRef:
        return await self._repository.final_diff(run_id)

    @activity.defn(name="cleanup_repository")
    async def cleanup_repository(self, run_id: UUID) -> ArtifactRef:
        return await self._repository.cleanup(run_id)

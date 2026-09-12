"""创建严格 TaskSpec 的 ingest Activities。"""

from __future__ import annotations

from uuid import uuid4

from temporalio import activity

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import CreateRunRequest, IngestResult, TaskSpec
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.repository_service import RepositoryService


class FinalizeTaskSpecInput(StrictModel):
    create_request_ref: ArtifactRef
    base_revision: str
    acceptance_criteria: tuple[str, ...]
    acceptance_criteria_source: str = "approved"


class IngestActivities:
    def __init__(self, *, artifact_store: ArtifactStore, repository: RepositoryService) -> None:
        self._artifacts = artifact_store
        self._repository = repository

    @activity.defn(name="ingest_task")
    async def ingest_task(self, request_ref: ArtifactRef) -> IngestResult:
        request = await self._read_request(request_ref)
        base_revision = await self._repository.resolve_revision(
            request.repository.url, request.repository.revision
        )
        if request.acceptance_criteria:
            task_ref = await self._store_task_spec(
                request_ref,
                request,
                base_revision,
                request.acceptance_criteria,
                "structured",
            )
            return IngestResult(
                repository_url=request.repository.url,
                base_revision=base_revision,
                proposed_acceptance_criteria=request.acceptance_criteria,
                acceptance_criteria_source="structured",
                requires_requirements_approval=False,
                task_spec_ref=task_ref,
            )

        proposed = (f"完成任务要求：{request.requirement.strip()}",)
        return IngestResult(
            repository_url=request.repository.url,
            base_revision=base_revision,
            proposed_acceptance_criteria=proposed,
            acceptance_criteria_source="heuristic",
            requires_requirements_approval=True,
            task_spec_ref=None,
        )

    @activity.defn(name="finalize_task_spec")
    async def finalize_task_spec(self, payload: FinalizeTaskSpecInput) -> ArtifactRef:
        request = await self._read_request(payload.create_request_ref)
        return await self._store_task_spec(
            payload.create_request_ref,
            request,
            payload.base_revision,
            payload.acceptance_criteria,
            "approved",
        )

    async def _read_request(self, request_ref: ArtifactRef) -> CreateRunRequest:
        caller = ArtifactCaller(
            tenant_id=request_ref.tenant_id,
            run_id=request_ref.run_id,
            role=None,
            service="ingest",
        )
        return CreateRunRequest.model_validate_json(
            await self._artifacts.get_bytes(request_ref, caller)
        )

    async def _store_task_spec(
        self,
        request_ref: ArtifactRef,
        request: CreateRunRequest,
        base_revision: str,
        criteria: tuple[str, ...],
        source: str,
    ) -> ArtifactRef:
        task = TaskSpec.model_validate(
            {
                "task_id": uuid4(),
                "run_id": request_ref.run_id,
                "tenant_id": request_ref.tenant_id,
                "repository_url": request.repository.url,
                "base_revision": base_revision,
                "requirement": request.requirement,
                "acceptance_criteria": criteria,
                "acceptance_criteria_source": source,
                "policy_profile": "default",
                "budget": request.budget,
                "approval_policy": request.approval_policy,
                "dependency_policy": request.dependency_policy,
                "qa": request.qa,
                "risk_level": request.risk_level,
            }
        )
        return await self._artifacts.put_bytes(
            ArtifactKind.TASK_SPEC,
            task.model_dump_json().encode("utf-8"),
            ArtifactMetadata(
                tenant_id=request_ref.tenant_id,
                run_id=request_ref.run_id,
                base_revision=base_revision,
                schema_version="1",
                input_artifact_ids=(request_ref.artifact_id,),
            ),
        )

"""Ingest Activity 把 HTTP 请求固定为不可变 TaskSpec。"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from repopilot.activities.ingest import FinalizeTaskSpecInput, IngestActivities
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import BudgetInput, CreateRunRequest, RepositoryInput, TaskSpec
from tests.fakes import MemoryArtifactStore


class FakeRepository:
    async def resolve_revision(self, repo_url: str, revision: str | None) -> str:
        del repo_url, revision
        return "a" * 40

    async def snapshot(self, task: TaskSpec) -> ArtifactRef:
        raise AssertionError(f"ingest 不应执行 snapshot: {task.task_id}")

    async def create_developer_context(
        self, work_item: object, integrated_patches: tuple[ArtifactRef, ...]
    ) -> ArtifactRef:
        raise AssertionError((work_item, integrated_patches))

    async def integrate_patch(
        self,
        proposal_ref: ArtifactRef,
        *,
        allowed_write_paths: tuple[str, ...],
        allow_dependency_changes: bool = False,
    ) -> ArtifactRef:
        raise AssertionError(
            (proposal_ref, allowed_write_paths, allow_dependency_changes)
        )

    async def final_diff(self, run_id: UUID) -> ArtifactRef:
        raise AssertionError(run_id)

    async def cleanup(self, run_id: UUID) -> ArtifactRef:
        raise AssertionError(run_id)


def _request(criteria: tuple[str, ...] | None) -> CreateRunRequest:
    return CreateRunRequest(
        repository=RepositoryInput(url="https://github.com/owner/repo"),
        requirement="fix it",
        acceptance_criteria=criteria,
        budget=BudgetInput(
            max_cost_usd=Decimal("2"),
            max_wall_time_seconds=600,
            max_model_calls=10,
            max_sandbox_seconds=300,
        ),
    )


async def _store_request(store: MemoryArtifactStore, request: CreateRunRequest) -> ArtifactRef:
    return await store.put_bytes(
        ArtifactKind.CREATE_RUN_REQUEST,
        request.model_dump_json().encode(),
        ArtifactMetadata(
            tenant_id=uuid4(),
            run_id=uuid4(),
            base_revision=None,
            schema_version="1",
        ),
    )


async def _read_task(store: MemoryArtifactStore, ref: ArtifactRef) -> TaskSpec:
    return TaskSpec.model_validate_json(
        await store.get_bytes(
            ref,
            ArtifactCaller(
                tenant_id=ref.tenant_id,
                run_id=ref.run_id,
                role=None,
                service="test",
            ),
        )
    )


async def test_structured_acceptance_criteria_create_task_spec_immediately() -> None:
    store = MemoryArtifactStore()
    request_ref = await _store_request(store, _request(("targeted test passes",)))
    activity = IngestActivities(artifact_store=store, repository=FakeRepository())

    result = await activity.ingest_task(request_ref)

    assert result.requires_requirements_approval is False
    assert result.task_spec_ref is not None
    task = await _read_task(store, result.task_spec_ref)
    assert task.acceptance_criteria == ("targeted test passes",)
    assert task.dependency_policy.allow_new_dependencies is False
    assert task.qa.required == ("acceptance_tests", "targeted_tests", "scope_check")


async def test_missing_acceptance_criteria_waits_for_human_then_finalizes() -> None:
    store = MemoryArtifactStore()
    request_ref = await _store_request(store, _request(None))
    activity = IngestActivities(artifact_store=store, repository=FakeRepository())

    result = await activity.ingest_task(request_ref)
    assert result.requires_requirements_approval is True
    assert result.task_spec_ref is None

    task_ref = await activity.finalize_task_spec(
        FinalizeTaskSpecInput(
            create_request_ref=request_ref,
            base_revision=result.base_revision,
            acceptance_criteria=("human approved criterion",),
        )
    )
    task = await _read_task(store, task_ref)
    assert task.acceptance_criteria_source == "approved"
    assert task.acceptance_criteria == ("human approved criterion",)

from decimal import Decimal
from uuid import uuid4

from repopilot.activities.planning import PlanChangeInput, PlanningActivities
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, RepositorySnapshot
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.plans import ChangePlan, PlannedFileChange
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.services.model_gateway import BudgetedModelGateway
from tests.fakes import MemoryArtifactStore, RecordingBudgetStore


async def test_planner_produces_validated_change_plan_artifact() -> None:
    store = MemoryArtifactStore()
    budget = RecordingBudgetStore()
    tenant_id, run_id, task_id, item_id = uuid4(), uuid4(), uuid4(), uuid4()
    base_revision = "a" * 40
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision=base_revision,
        schema_version="1",
    )
    source_ref = await store.put_bytes(ArtifactKind.SOURCE_ARCHIVE, b"source", metadata)
    symbol_ref = await store.put_bytes(ArtifactKind.SYMBOL_INDEX, b"{}", metadata)
    task = TaskSpec(
        task_id=task_id,
        run_id=run_id,
        tenant_id=tenant_id,
        repository_url="https://github.com/owner/repo",
        base_revision=base_revision,
        requirement="fix bug",
        acceptance_criteria=("tests pass",),
        acceptance_criteria_source="structured",
        policy_profile="default",
        budget=BudgetInput(
            max_cost_usd=Decimal("1"),
            max_wall_time_seconds=600,
            max_model_calls=5,
            max_sandbox_seconds=300,
        ),
    )
    task_ref = await store.put_bytes(
        ArtifactKind.TASK_SPEC, task.model_dump_json().encode(), metadata
    )
    snapshot = RepositorySnapshot(
        repository_url=task.repository_url,
        base_revision=base_revision,
        source_archive_ref=source_ref,
        primary_language="python",
        python_versions=("3.12",),
        dependency_manifest_paths=("pyproject.toml",),
        test_config_paths=("pyproject.toml",),
        forbidden_paths=(),
        symbol_index_ref=symbol_ref,
    )
    snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        snapshot.model_dump_json().encode(),
        metadata,
    )
    expected = ChangePlan(
        plan_id=uuid4(),
        version=1,
        supersedes_plan_id=None,
        files=(
            PlannedFileChange(
                work_item_id=item_id,
                path="src/app.py",
                operation="modify",
                owner="developer-1",
                responsibility="fix bug",
                required_interfaces=(),
            ),
        ),
        dependency_edges=(),
        risk_flags=(),
    )
    provider = FakeModelProvider([expected], store)
    planner = PlanningActivities(
        artifact_store=store,
        gateway=BudgetedModelGateway(provider, budget),
        model="fake-model",
        reservation_usd=Decimal("0.10"),
    )

    result = await planner.plan_change(
        PlanChangeInput(task_spec_ref=task_ref, repository_snapshot_ref=snapshot_ref)
    )

    caller = ArtifactCaller(
        tenant_id=tenant_id,
        run_id=run_id,
        role=None,
        service="test",
    )
    persisted = ChangePlan.model_validate_json(await store.get_bytes(result.plan_ref, caller))
    assert persisted == expected
    assert result.plan_ref.kind is ArtifactKind.CHANGE_PLAN
    assert result.risk_level.value == "low"
    assert len(provider.requests) == 1
    assert len(budget.settled) == 1

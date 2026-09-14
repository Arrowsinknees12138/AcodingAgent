import io
import tarfile
from decimal import Decimal
from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError

from repopilot.activities.planning import PlanChangeInput, PlanningActivities
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, RepositorySnapshot
from repopilot.domain.enums import ArtifactKind, RiskFlag
from repopilot.domain.plans import ChangePlan, PlannedFileChange
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.domain.verification import TestSummary, VerificationReport
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
    assert len(result.work_items) == 1
    assert result.work_items[0].work_item_id == item_id
    assert result.work_items[0].allowed_write_paths == ("src/app.py",)
    assert result.waves == ((item_id,),)
    assert len(provider.requests) == 1
    assert len(budget.settled) == 1

    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        content = b"def broken():\n    return 1\n"
        info = tarfile.TarInfo("src/app.py")
        info.size = len(content)
        tar.addfile(info, io.BytesIO(content))
    candidate_source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE, archive.getvalue(), metadata
    )
    candidate_snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        snapshot.model_copy(update={"source_archive_ref": candidate_source_ref})
        .model_dump_json()
        .encode(),
        metadata,
    )
    log_ref = await store.put_bytes(ArtifactKind.LOG, b"SEALED TEST SOURCE SECRET", metadata)
    baseline_ref = await store.put_bytes(ArtifactKind.BASELINE_REPORT, b"{}", metadata)
    feedback_ref = await store.put_bytes(
        ArtifactKind.VERIFICATION_REPORT,
        VerificationReport(
            candidate_revision="b" * 40,
            baseline_report_ref=baseline_ref,
            passed=False,
            findings=(),
            baseline_summary=TestSummary(passed=0, failed=0, skipped=0, failed_test_ids=()),
            candidate_summary=TestSummary(passed=0, failed=1, skipped=0, failed_test_ids=()),
            regression_test_ids=(),
            regression_count=0,
            stdout_ref=log_ref,
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    repair_planner = PlanningActivities(
        artifact_store=store,
        gateway=BudgetedModelGateway(FakeModelProvider([expected], store), RecordingBudgetStore()),
        model="fake-model",
        reservation_usd=Decimal("0.10"),
    )
    repair = await repair_planner.plan_change(
        PlanChangeInput(
            task_spec_ref=task_ref,
            repository_snapshot_ref=candidate_snapshot_ref,
            attempt=2,
            repair_feedback_ref=feedback_ref,
            allowed_repair_paths=("src/app.py",),
        )
    )
    assert repair.work_items[0].kind == "repair"
    assert repair.work_items[0].attempt == 2
    trajectories = [
        store.content[ref.artifact_id] for ref in store.refs if ref.kind is ArtifactKind.TRAJECTORY
    ]
    assert b"SEALED TEST SOURCE SECRET" not in trajectories[-1]

    with pytest.raises(ApplicationError, match="PLAN_INVALID"):
        await PlanningActivities(
            artifact_store=store,
            gateway=BudgetedModelGateway(
                FakeModelProvider(
                    [
                        expected.model_copy(
                            update={
                                "files": (
                                    expected.files[0].model_copy(
                                        update={"path": "src/unplanned.py", "operation": "create"}
                                    ),
                                )
                            }
                        )
                    ],
                    store,
                ),
                RecordingBudgetStore(),
            ),
            model="fake-model",
            reservation_usd=Decimal("0.10"),
        ).plan_change(
            PlanChangeInput(
                task_spec_ref=task_ref,
                repository_snapshot_ref=candidate_snapshot_ref,
                attempt=3,
                repair_feedback_ref=feedback_ref,
                allowed_repair_paths=("src/app.py",),
            )
        )

    expanded = expected.model_copy(
        update={
            "files": (
                *expected.files,
                expected.files[0].model_copy(
                    update={"path": "src/new.py", "operation": "create"}
                ),
            ),
            "scope_expansion_reason": "The repair requires a helper absent from the first plan",
        }
    )
    expanded_planner = PlanningActivities(
        artifact_store=store,
        gateway=BudgetedModelGateway(FakeModelProvider([expanded], store), RecordingBudgetStore()),
        model="fake-model",
        reservation_usd=Decimal("0.10"),
    )
    expanded_result = await expanded_planner.plan_change(
        PlanChangeInput(
            task_spec_ref=task_ref,
            repository_snapshot_ref=candidate_snapshot_ref,
            attempt=3,
            repair_feedback_ref=feedback_ref,
            allowed_repair_paths=("src/app.py",),
            allow_scope_expansion=True,
        )
    )
    assert expanded_result.scope_expansion_paths == ("src/new.py",)
    assert expanded_result.risk_level.value == "medium"
    stored_plan = ChangePlan.model_validate_json(
        await store.get_bytes(expanded_result.plan_ref, caller)
    )
    assert RiskFlag.LARGE_SCOPE in stored_plan.risk_flags
    assert stored_plan.scope_expansion_reason == expanded.scope_expansion_reason

    with pytest.raises(ApplicationError, match="PLAN_INVALID"):
        await PlanningActivities(
            artifact_store=store,
            gateway=BudgetedModelGateway(
                FakeModelProvider(
                    [expanded.model_copy(update={"scope_expansion_reason": None})], store
                ),
                RecordingBudgetStore(),
            ),
            model="fake-model",
            reservation_usd=Decimal("0.10"),
        ).plan_change(
            PlanChangeInput(
                task_spec_ref=task_ref,
                repository_snapshot_ref=candidate_snapshot_ref,
                attempt=3,
                repair_feedback_ref=feedback_ref,
                allowed_repair_paths=("src/app.py",),
                allow_scope_expansion=True,
            )
        )

import io
import json
import tarfile
from decimal import Decimal
from uuid import uuid4

import pytest

from repopilot.activities.qa import (
    DesignSealedTestsInput,
    QaAcceptanceMapping,
    QaActivities,
    QaSealedTestDesign,
    _canonicalize_design,
)
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, RepositorySnapshot
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.domain.verification import (
    BaselineReport,
    SealedTestBaselineReport,
    SealedTestFile,
    TestCaseSpec,
    TestPlan,
    TestSummary,
)
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.services.model_gateway import BudgetedModelGateway
from tests.fakes import MemoryArtifactStore, RecordingBudgetStore


async def test_qa_persists_deterministic_sealed_bundle_and_test_plan() -> None:
    store = MemoryArtifactStore()
    budget = RecordingBudgetStore()
    tenant_id, run_id = uuid4(), uuid4()
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
        task_id=uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        repository_url="https://github.com/owner/repo",
        base_revision=base_revision,
        requirement="fix add",
        acceptance_criteria=("add(1, 2) 返回 3",),
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
        dependency_manifest_paths=(),
        test_config_paths=(),
        forbidden_paths=(),
        symbol_index_ref=symbol_ref,
    )
    snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        snapshot.model_dump_json().encode(),
        metadata,
    )
    details_ref = await store.put_bytes(ArtifactKind.LOG, b"baseline details", metadata)
    baseline_ref = await store.put_bytes(
        ArtifactKind.BASELINE_REPORT,
        BaselineReport(
            base_revision=base_revision,
            runnable=True,
            command=("python", "-m", "pytest"),
            summary=TestSummary(passed=2, failed=0, skipped=0, failed_test_ids=()),
            details_ref=details_ref,
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    design = QaSealedTestDesign(
        files=(
            SealedTestFile(
                path=".repopilot/sealed_tests/test_add.py",
                content="def test_add():\n    assert 1 + 2 == 3\n",
            ),
        ),
        cases=(
            TestCaseSpec(
                name="test_add",
                purpose="acceptance",
                expected_on_base="fail",
            ),
        ),
        acceptance_mapping=(QaAcceptanceMapping(criterion_index=0, test_names=("test_add",)),),
    )
    corrected_design = design.model_copy(
        update={
            "cases": (TestCaseSpec(name="test_add", purpose="acceptance", expected_on_base="pass"),)
        }
    )
    provider = FakeModelProvider([design, corrected_design], store)
    qa = QaActivities(
        artifact_store=store,
        gateway=BudgetedModelGateway(provider, budget),
        model="fake-model",
        reservation_usd=Decimal("0.10"),
    )

    plan_ref = await qa.design_sealed_tests(
        DesignSealedTestsInput(
            task_spec_ref=task_ref,
            repository_snapshot_ref=snapshot_ref,
            baseline_report_ref=baseline_ref,
        )
    )

    caller = ArtifactCaller(
        tenant_id=tenant_id,
        run_id=run_id,
        role=None,
        service="test",
    )
    plan = TestPlan.model_validate_json(await store.get_bytes(plan_ref, caller))
    assert plan.origin == "sealed"
    assert plan.cases[0].name == "test_add"
    assert plan.acceptance_mapping[0].acceptance_criterion == "add(1, 2) 返回 3"
    bundle = await store.get_bytes(plan.test_bundle_ref, caller)
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        assert archive.getnames() == [".repopilot/sealed_tests/test_add.py"]
        extracted = archive.extractfile(archive.getmembers()[0])
        assert extracted is not None
        assert b"assert 1 + 2 == 3" in extracted.read()
    initial_messages = json.loads(await store.get_bytes(provider.requests[0].messages_ref, caller))
    assert initial_messages["messages"][0]["content"]["existing_test_baseline"]["summary"] == {
        "passed": 2,
        "failed": 0,
        "skipped": 0,
        "failed_test_ids": [],
    }
    assert initial_messages["messages"][0]["content"]["revision_feedback"] is None

    sealed_report_ref = await store.put_bytes(
        ArtifactKind.SEALED_TEST_BASELINE_REPORT,
        SealedTestBaselineReport(
            base_revision=base_revision,
            valid=False,
            runnable=True,
            command=("python", "-m", "pytest", ".repopilot/sealed_tests"),
            summary=TestSummary(passed=1, failed=0, skipped=0, failed_test_ids=()),
            case_outcomes={"test_add": "passed"},
            mismatches=("test_add: expected failed, observed passed",),
            details_ref=details_ref,
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    revised_ref = await qa.design_sealed_tests(
        DesignSealedTestsInput(
            task_spec_ref=task_ref,
            repository_snapshot_ref=snapshot_ref,
            baseline_report_ref=baseline_ref,
            previous_test_plan_ref=plan_ref,
            sealed_baseline_report_ref=sealed_report_ref,
            attempt=2,
        )
    )
    revised_plan = TestPlan.model_validate_json(await store.get_bytes(revised_ref, caller))
    assert revised_plan.cases[0].expected_on_base == "pass"
    revision_messages = json.loads(await store.get_bytes(provider.requests[1].messages_ref, caller))
    feedback = revision_messages["messages"][0]["content"]["revision_feedback"]
    assert feedback["observed_outcomes"] == {"test_add": "passed"}
    assert feedback["mismatches"] == ["test_add: expected failed, observed passed"]
    assert "assert 1 + 2 == 3" in feedback["previous_test_files"]["files"][0]["content"]
    assert len(provider.requests) == 2
    assert len(budget.settled) == 2


def _task_with_criteria(criteria: tuple[str, ...]) -> TaskSpec:
    return TaskSpec(
        task_id=uuid4(),
        run_id=uuid4(),
        tenant_id=uuid4(),
        repository_url="https://github.com/owner/repo",
        base_revision="a" * 40,
        requirement="fix add",
        acceptance_criteria=criteria,
        acceptance_criteria_source="structured",
        policy_profile="default",
        budget=BudgetInput(
            max_cost_usd=Decimal("1"),
            max_wall_time_seconds=600,
            max_model_calls=5,
            max_sandbox_seconds=300,
        ),
    )


def _indexed_design(indices: tuple[int, ...]) -> QaSealedTestDesign:
    return QaSealedTestDesign(
        files=(
            SealedTestFile(
                path=".repopilot/sealed_tests/test_add.py",
                content="def test_add():\n    assert 1 + 2 == 3\n",
            ),
        ),
        cases=(TestCaseSpec(name="test_add", purpose="acceptance", expected_on_base="fail"),),
        acceptance_mapping=tuple(
            QaAcceptanceMapping(criterion_index=index, test_names=("test_add",))
            for index in indices
        ),
    )


def test_qa_maps_indices_back_to_exact_original_criteria() -> None:
    criteria = ("add(1, 2) 返回 3", "现有测试不得出现新增失败")
    task = _task_with_criteria(criteria)

    design = _canonicalize_design(_indexed_design((1, 0)), task)

    assert tuple(item.acceptance_criterion for item in design.acceptance_mapping) == criteria
    assert tuple(item.test_names for item in design.acceptance_mapping) == (
        ("test_add",),
        ("test_add",),
    )


@pytest.mark.parametrize("indices", [(0,), (0, 0), (0, 2)])
def test_qa_rejects_missing_duplicate_or_out_of_range_criterion_indices(
    indices: tuple[int, ...],
) -> None:
    task = _task_with_criteria(("add(1, 2) 返回 3", "现有测试不得出现新增失败"))

    with pytest.raises(ValueError, match="acceptance criterion"):
        _canonicalize_design(_indexed_design(indices), task)

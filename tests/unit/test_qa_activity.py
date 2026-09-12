import io
import tarfile
from decimal import Decimal
from uuid import uuid4

from repopilot.activities.qa import DesignSealedTestsInput, QaActivities
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, RepositorySnapshot
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.domain.verification import (
    AcceptanceTestMapping,
    SealedTestDesign,
    SealedTestFile,
    TestCaseSpec,
    TestPlan,
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
        acceptance_criteria=("add(1, 2) returns 3",),
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
    design = SealedTestDesign(
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
        acceptance_mapping=(
            AcceptanceTestMapping(
                acceptance_criterion="add(1, 2) returns 3",
                test_names=("test_add",),
            ),
        ),
    )
    provider = FakeModelProvider([design], store)
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
    bundle = await store.get_bytes(plan.test_bundle_ref, caller)
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        assert archive.getnames() == [".repopilot/sealed_tests/test_add.py"]
        extracted = archive.extractfile(archive.getmembers()[0])
        assert extracted is not None
        assert b"assert 1 + 2 == 3" in extracted.read()
    assert len(provider.requests) == 1
    assert len(budget.settled) == 1

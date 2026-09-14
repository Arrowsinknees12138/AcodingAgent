from __future__ import annotations

import io
import tarfile
from decimal import Decimal
from uuid import uuid4

from repopilot.activities.investigator import InvestigateInput, InvestigatorActivities
from repopilot.domain.agents import AgentTurn
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, RepositorySnapshot
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.investigation import InvestigationEvidence, InvestigationReport
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.domain.verification import TestSummary, VerificationReport
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.services.model_gateway import BudgetedModelGateway
from tests.fakes import MemoryArtifactStore, RecordingBudgetStore


async def test_investigator_searches_and_persists_evidence_without_raw_log() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    archive_buffer = io.BytesIO()
    with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
        source = b"def add(a, b):\n    return a - b\n"
        member = tarfile.TarInfo("calc.py")
        member.size = len(source)
        archive.addfile(member, io.BytesIO(source))
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE, archive_buffer.getvalue(), metadata
    )
    symbol_ref = await store.put_bytes(ArtifactKind.SYMBOL_INDEX, b"{}", metadata)
    snapshot = RepositorySnapshot(
        repository_url="https://github.com/owner/repo",
        base_revision="a" * 40,
        source_archive_ref=source_ref,
        primary_language="python",
        python_versions=("3.12",),
        dependency_manifest_paths=(),
        test_config_paths=(),
        forbidden_paths=(),
        symbol_index_ref=symbol_ref,
    )
    snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT, snapshot.model_dump_json().encode(), metadata
    )
    task = TaskSpec(
        task_id=uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        repository_url=snapshot.repository_url,
        base_revision=snapshot.base_revision,
        requirement="fix addition",
        acceptance_criteria=("add returns a sum",),
        acceptance_criteria_source="structured",
        policy_profile="default",
        budget=BudgetInput(
            max_cost_usd=Decimal("1"),
            max_wall_time_seconds=600,
            max_model_calls=10,
            max_sandbox_seconds=300,
        ),
    )
    task_ref = await store.put_bytes(
        ArtifactKind.TASK_SPEC, task.model_dump_json().encode(), metadata
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
    report = InvestigationReport(
        root_cause="The add function subtracts its operands",
        evidence=(InvestigationEvidence(path="calc.py", line=2, observation="return a - b"),),
        suggested_paths=("calc.py",),
        recommendation="Change the operator to addition",
        confidence=0.95,
    )
    provider = FakeModelProvider(
        [
            AgentTurn(tool="search_code", arguments={"query": "def add"}),
            AgentTurn(tool="read_file", arguments={"path": "calc.py"}),
            AgentTurn(
                tool="submit_investigation", arguments={"report": report.model_dump(mode="json")}
            ),
            AgentTurn(tool="finish", arguments={"status": "succeeded"}),
        ],
        store,
    )
    result_ref = await InvestigatorActivities(
        artifact_store=store,
        gateway=BudgetedModelGateway(provider, RecordingBudgetStore()),
        model="fake-model",
        reservation_usd=Decimal("0.10"),
    ).investigate_failure(
        InvestigateInput(
            task_spec_ref=task_ref,
            repository_snapshot_ref=snapshot_ref,
            repair_feedback_ref=feedback_ref,
            attempt=2,
        )
    )
    caller = ArtifactCaller(tenant_id=tenant_id, run_id=run_id, role=None, service="test")
    assert result_ref.kind is ArtifactKind.INVESTIGATION
    assert (
        InvestigationReport.model_validate_json(await store.get_bytes(result_ref, caller)) == report
    )
    trajectories = [
        store.content[ref.artifact_id] for ref in store.refs if ref.kind is ArtifactKind.TRAJECTORY
    ]
    assert all(b"SEALED TEST SOURCE SECRET" not in item for item in trajectories)
    assert len(provider.requests) == 4

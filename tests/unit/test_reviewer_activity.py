from decimal import Decimal
from uuid import uuid4

from repopilot.activities.reviewer import ReviewCandidateInput, ReviewerActivities
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.domain.verification import (
    ReviewDecision,
    ReviewFinding,
    TestSummary,
    VerificationReport,
)
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.services.model_gateway import BudgetedModelGateway
from tests.fakes import MemoryArtifactStore, RecordingBudgetStore


async def test_reviewer_persists_comments_without_blocking_approval() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="b" * 40,
        schema_version="1",
    )
    task_ref = await store.put_bytes(
        ArtifactKind.TASK_SPEC,
        TaskSpec(
            task_id=uuid4(),
            run_id=run_id,
            tenant_id=tenant_id,
            repository_url="https://github.com/owner/repo",
            base_revision="a" * 40,
            requirement="fix bug",
            acceptance_criteria=("tests pass",),
            acceptance_criteria_source="structured",
            policy_profile="default",
            budget=BudgetInput(
                max_cost_usd=Decimal("1"),
                max_wall_time_seconds=600,
                max_model_calls=10,
                max_sandbox_seconds=300,
            ),
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    diff_ref = await store.put_bytes(
        ArtifactKind.PATCH,
        b"diff --git a/src/app.py b/src/app.py\n",
        metadata,
    )
    log_ref = await store.put_bytes(ArtifactKind.LOG, b"ok", metadata)
    baseline_ref = await store.put_bytes(ArtifactKind.BASELINE_REPORT, b"{}", metadata)
    verification_ref = await store.put_bytes(
        ArtifactKind.VERIFICATION_REPORT,
        VerificationReport(
            candidate_revision="b" * 40,
            baseline_report_ref=baseline_ref,
            passed=True,
            findings=(),
            baseline_summary=TestSummary(
                passed=1,
                failed=0,
                skipped=0,
                failed_test_ids=(),
            ),
            candidate_summary=TestSummary(
                passed=2,
                failed=0,
                skipped=0,
                failed_test_ids=(),
            ),
            regression_test_ids=(),
            regression_count=0,
            stdout_ref=log_ref,
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    expected = ReviewDecision(
        decision="approve",
        findings=(
            ReviewFinding(
                finding_id=uuid4(),
                file_path="src/app.py",
                category="style",
                disposition="comment",
                message="Minor style suggestion.",
            ),
        ),
        rationale="No blocker found.",
    )
    provider = FakeModelProvider([expected], store)

    result = await ReviewerActivities(
        artifact_store=store,
        gateway=BudgetedModelGateway(provider, RecordingBudgetStore()),
        model="fake-model",
        reservation_usd=Decimal("0.10"),
    ).review_candidate(
        ReviewCandidateInput(
            task_spec_ref=task_ref,
            diff_ref=diff_ref,
            verification_ref=verification_ref,
        )
    )
    persisted = ReviewDecision.model_validate_json(
        await store.get_bytes(
            result.review_ref,
            ArtifactCaller(
                tenant_id=tenant_id,
                run_id=run_id,
                role=None,
                service="test",
            ),
        )
    )

    assert result.decision == "approve"
    assert persisted == expected
    assert result.review_ref.kind is ArtifactKind.REVIEW_DECISION

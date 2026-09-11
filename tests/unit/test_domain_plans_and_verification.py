"""ChangePlan/WorkItem 与 Verification 相关契约的基本构造校验。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from repopilot.domain.artifacts import ArtifactRef, build_object_key
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.plans import ChangePlan, PlannedFileChange, WorkItem
from repopilot.domain.verification import TestSummary, VerificationReport

VALID_SHA256 = "c" * 64
VALID_COMMIT_SHA = "d" * 40


def _make_ref(kind: ArtifactKind = ArtifactKind.PATCH) -> ArtifactRef:
    tenant_id, run_id = uuid4(), uuid4()
    return ArtifactRef(
        artifact_id=uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        kind=kind,
        schema_version="1",
        object_key=build_object_key(tenant_id, run_id, kind, VALID_SHA256),
        sha256=VALID_SHA256,
        size_bytes=1,
        base_revision=VALID_COMMIT_SHA,
        input_artifact_ids=(),
        created_at=datetime.now(UTC),
    )


def test_work_item_requires_declared_fields() -> None:
    work_item_id = uuid4()
    item = WorkItem(
        work_item_id=work_item_id,
        run_id=uuid4(),
        kind="code",
        dependencies=(),
        allowed_write_paths=("src/foo.py",),
        read_paths=("src/foo.py",),
        owner="developer-1",
        attempt=1,
    )
    assert item.work_item_id == work_item_id


def test_change_plan_rejects_unknown_operation_literal() -> None:
    with pytest.raises(ValidationError):
        PlannedFileChange(
            work_item_id=uuid4(),
            path="src/foo.py",
            operation="rename",  # type: ignore[arg-type]
            owner="developer-1",
            responsibility="add foo",
            required_interfaces=(),
        )


def test_change_plan_holds_files_and_edges() -> None:
    a, b = uuid4(), uuid4()
    plan = ChangePlan(
        plan_id=uuid4(),
        version=1,
        supersedes_plan_id=None,
        files=(
            PlannedFileChange(
                work_item_id=a,
                path="src/foo.py",
                operation="create",
                owner="developer-1",
                responsibility="add foo",
                required_interfaces=(),
            ),
        ),
        dependency_edges=((a, b),),
        risk_flags=(),
    )
    assert plan.dependency_edges == ((a, b),)


def test_verification_report_tracks_regressions() -> None:
    baseline = TestSummary(passed=1, failed=0, skipped=0, failed_test_ids=())
    candidate = TestSummary(passed=0, failed=1, skipped=0, failed_test_ids=("test_x",))
    report = VerificationReport(
        candidate_revision=VALID_COMMIT_SHA,
        baseline_report_ref=_make_ref(ArtifactKind.BASELINE_REPORT),
        passed=False,
        findings=(),
        baseline_summary=baseline,
        candidate_summary=candidate,
        regression_test_ids=("test_x",),
        regression_count=1,
        report_ref=_make_ref(ArtifactKind.VERIFICATION_REPORT),
        stdout_ref=None,
    )
    assert report.regression_count == 1
    assert report.passed is False

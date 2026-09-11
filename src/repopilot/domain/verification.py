"""测试计划与验证结果契约（实施设计第 7.5、15 节）。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef


class TestCaseSpec(StrictModel):
    name: str
    purpose: Literal["bug_reproduction", "acceptance", "regression"]
    expected_on_base: Literal["pass", "fail", "skip", "not_applicable"]
    expected_on_candidate: Literal["pass"] = "pass"


class AcceptanceTestMapping(StrictModel):
    acceptance_criterion: str
    test_names: tuple[str, ...]


class TestPlan(StrictModel):
    test_plan_id: UUID
    origin: Literal["sealed", "post_patch"]
    test_bundle_ref: ArtifactRef
    cases: tuple[TestCaseSpec, ...]
    acceptance_mapping: tuple[AcceptanceTestMapping, ...]


class TestSummary(StrictModel):
    passed: int
    failed: int
    skipped: int
    failed_test_ids: tuple[str, ...]


class BaselineReport(StrictModel):
    base_revision: str
    runnable: bool
    command: tuple[str, ...]
    summary: TestSummary
    details_ref: ArtifactRef


class VerificationFinding(StrictModel):
    finding_id: UUID
    file_path: str | None
    severity: Literal["blocker", "major", "minor"]
    category: Literal[
        "syntax",
        "test",
        "regression",
        "type_check",
        "lint",
        "security",
        "compatibility",
        "correctness",
    ]
    message: str


class VerificationReport(StrictModel):
    candidate_revision: str
    baseline_report_ref: ArtifactRef
    passed: bool
    findings: tuple[VerificationFinding, ...]
    baseline_summary: TestSummary
    candidate_summary: TestSummary
    regression_test_ids: tuple[str, ...]
    regression_count: int
    report_ref: ArtifactRef
    stdout_ref: ArtifactRef | None


class ReviewDecision(StrictModel):
    decision: Literal["approve", "request_changes", "reject"]
    findings: tuple[VerificationFinding, ...]
    rationale: str

"""测试计划与验证结果契约（实施设计第 7.5、15 节）。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import model_validator

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.policies import STANDARD_QA_CHECKS, QaCheck


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


class QaCheckResult(StrictModel):
    check: QaCheck
    outcome: Literal["passed", "failed"]
    details: str


class QaReport(StrictModel):
    level: Literal["standard"] = "standard"
    checks: tuple[QaCheckResult, ...]
    passed: bool

    @model_validator(mode="after")
    def _all_standard_checks_are_reported(self) -> QaReport:
        names = tuple(result.check for result in self.checks)
        if len(set(names)) != len(names):
            raise ValueError("QA report 不能重复同一种检查")
        missing = set(STANDARD_QA_CHECKS).difference(names)
        if missing:
            raise ValueError(f"QA report 缺少必需检查: {', '.join(sorted(missing))}")
        expected_passed = all(result.outcome == "passed" for result in self.checks)
        if self.passed != expected_passed:
            raise ValueError("QA report 的 passed 必须与各项检查结果一致")
        return self


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
    # 承载本模型的外层 ArtifactRef 就是 report ref；序列化内容不能自引用。
    report_ref: ArtifactRef | None = None
    stdout_ref: ArtifactRef | None


ReviewBlockCategory = Literal[
    "acceptance_criteria",
    "scope",
    "regression",
    "security",
    "error_handling",
    "api_contract",
    "unnecessary_dependency",
    "test_bypass",
    "test_change_hides_bug",
]
ReviewCommentCategory = Literal["naming", "refactoring", "documentation", "style"]
ReviewCategory = ReviewBlockCategory | ReviewCommentCategory

_REVIEW_BLOCK_CATEGORIES = frozenset(
    {
        "acceptance_criteria",
        "scope",
        "regression",
        "security",
        "error_handling",
        "api_contract",
        "unnecessary_dependency",
        "test_bypass",
        "test_change_hides_bug",
    }
)


class ReviewFinding(StrictModel):
    finding_id: UUID
    file_path: str | None
    category: ReviewCategory
    disposition: Literal["block", "comment"]
    message: str

    @model_validator(mode="after")
    def _disposition_matches_category(self) -> ReviewFinding:
        expected = "block" if self.category in _REVIEW_BLOCK_CATEGORIES else "comment"
        if self.disposition != expected:
            raise ValueError(
                f"Reviewer category={self.category} 必须使用 disposition={expected}"
            )
        return self


class ReviewDecision(StrictModel):
    decision: Literal["approve", "request_changes", "reject"]
    findings: tuple[ReviewFinding, ...]
    rationale: str

    @model_validator(mode="after")
    def _decision_matches_blockers(self) -> ReviewDecision:
        has_blocker = any(finding.disposition == "block" for finding in self.findings)
        if has_blocker and self.decision == "approve":
            raise ValueError("存在 BLOCK finding 时 Reviewer 不能 approve")
        if not has_blocker and self.decision != "approve":
            raise ValueError("没有 BLOCK finding 时 Reviewer 必须 approve")
        return self

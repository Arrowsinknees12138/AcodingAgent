"""QA 必检项和 Reviewer 阻断边界。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from repopilot.domain.verification import (
    QaCheckResult,
    QaReport,
    ReviewDecision,
    ReviewFinding,
)


def _qa_result(check: str, outcome: str = "passed") -> QaCheckResult:
    return QaCheckResult.model_validate(
        {"check": check, "outcome": outcome, "details": f"{check}: {outcome}"}
    )


def test_standard_qa_requires_acceptance_targeted_and_scope_checks() -> None:
    report = QaReport(
        checks=(
            _qa_result("acceptance_tests"),
            _qa_result("targeted_tests"),
            _qa_result("scope_check"),
        ),
        passed=True,
    )
    assert report.passed


def test_standard_qa_rejects_missing_scope_check() -> None:
    with pytest.raises(ValidationError):
        QaReport(
            checks=(
                _qa_result("acceptance_tests"),
                _qa_result("targeted_tests"),
            ),
            passed=True,
        )


def test_standard_qa_cannot_claim_pass_when_a_check_failed() -> None:
    with pytest.raises(ValidationError):
        QaReport(
            checks=(
                _qa_result("acceptance_tests"),
                _qa_result("targeted_tests", "failed"),
                _qa_result("scope_check"),
            ),
            passed=True,
        )


def test_style_and_naming_findings_are_comments_only() -> None:
    finding = ReviewFinding(
        finding_id=uuid4(),
        file_path="src/example.py",
        category="style",
        disposition="comment",
        message="Could simplify formatting.",
    )
    decision = ReviewDecision(decision="approve", findings=(finding,), rationale="non-blocking")
    assert decision.decision == "approve"


def test_minor_error_handling_finding_can_be_a_comment() -> None:
    finding = ReviewFinding(
        finding_id=uuid4(),
        file_path="src/example.py",
        category="error_handling",
        disposition="comment",
        message="Optional improvement to an already handled error path.",
    )
    decision = ReviewDecision(decision="approve", findings=(finding,), rationale="No blocker")
    assert decision.findings[0].disposition == "comment"


@pytest.mark.parametrize(
    "category",
    [
        "acceptance_criteria",
        "scope",
        "regression",
        "security",
        "error_handling",
        "api_contract",
        "unnecessary_dependency",
        "test_bypass",
        "test_change_hides_bug",
    ],
)
def test_configured_reviewer_categories_are_blocking(category: str) -> None:
    finding = ReviewFinding.model_validate(
        {
            "finding_id": uuid4(),
            "file_path": None,
            "category": category,
            "disposition": "block",
            "message": "must fix",
        }
    )
    decision = ReviewDecision(
        decision="request_changes",
        findings=(finding,),
        rationale="blocking issue",
    )
    assert decision.decision == "request_changes"


def test_reviewer_cannot_block_on_refactoring_preference() -> None:
    with pytest.raises(ValidationError):
        ReviewFinding(
            finding_id=uuid4(),
            file_path=None,
            category="refactoring",
            disposition="block",
            message="I prefer another abstraction.",
        )


def test_reviewer_cannot_approve_with_blocker() -> None:
    finding = ReviewFinding(
        finding_id=uuid4(),
        file_path=None,
        category="security",
        disposition="block",
        message="unsafe behavior",
    )
    with pytest.raises(ValidationError):
        ReviewDecision(decision="approve", findings=(finding,), rationale="looks okay")

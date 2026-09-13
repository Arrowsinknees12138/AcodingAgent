"""Minimal, role-safe failure evidence for repair planning and development."""

from __future__ import annotations

from repopilot.domain.enums import ArtifactKind
from repopilot.domain.verification import ReviewDecision, VerificationReport


def summarize_repair_feedback(kind: ArtifactKind, content: bytes) -> dict[str, object]:
    if kind is ArtifactKind.VERIFICATION_REPORT:
        report = VerificationReport.model_validate_json(content)
        return {
            "source": "verification",
            "findings": [
                {
                    "path": finding.file_path,
                    "category": finding.category,
                    "message": finding.message[:1000],
                }
                for finding in report.findings[:20]
            ],
            "regression_count": report.regression_count,
        }
    if kind is not ArtifactKind.REVIEW_DECISION:
        raise ValueError("repair feedback must be verification or review")
    review = ReviewDecision.model_validate_json(content)
    return {
        "source": "review",
        "findings": [
            {
                "path": finding.file_path,
                "category": finding.category,
                "message": finding.message[:1000],
            }
            for finding in review.findings[:20]
            if finding.disposition == "block"
        ],
    }

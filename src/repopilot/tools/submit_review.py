"""Reviewer-only structured decision submission tool."""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.domain import StrictModel
from repopilot.domain.verification import ReviewDecision
from repopilot.tools.registry import ToolContext


class SubmitReviewRequest(StrictModel):
    review: ReviewDecision


class SubmitReviewResult(StrictModel):
    accepted: bool
    decision: str
    blockers: int


class SubmitReviewTool:
    name = "submit_review"
    input_model: type[BaseModel] = SubmitReviewRequest

    def __init__(self) -> None:
        self.review: ReviewDecision | None = None

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = SubmitReviewRequest.model_validate(request)
        if self.review is not None:
            raise ValueError("review already submitted")
        self.review = typed.review
        return SubmitReviewResult(
            accepted=True,
            decision=typed.review.decision,
            blockers=sum(item.disposition == "block" for item in typed.review.findings),
        )

"""Investigator-only report submission tool."""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.domain import StrictModel
from repopilot.domain.investigation import InvestigationReport
from repopilot.tools.registry import ToolContext


class SubmitInvestigationRequest(StrictModel):
    report: InvestigationReport


class SubmitInvestigationResult(StrictModel):
    accepted: bool


class SubmitInvestigationTool:
    name = "submit_investigation"
    input_model: type[BaseModel] = SubmitInvestigationRequest

    def __init__(self) -> None:
        self.report: InvestigationReport | None = None

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = SubmitInvestigationRequest.model_validate(request)
        if self.report is not None:
            raise ValueError("investigation already submitted")
        self.report = typed.report
        return SubmitInvestigationResult(accepted=True)

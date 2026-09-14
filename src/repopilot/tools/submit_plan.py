"""Planner-only structured plan submission tool."""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.domain import StrictModel
from repopilot.domain.plans import ChangePlan
from repopilot.services.scheduler import validate_change_plan
from repopilot.tools.registry import ToolContext


class SubmitPlanRequest(StrictModel):
    plan: ChangePlan


class SubmitPlanResult(StrictModel):
    accepted: bool
    changed_paths: tuple[str, ...]


class SubmitPlanTool:
    name = "submit_plan"
    input_model: type[BaseModel] = SubmitPlanRequest

    def __init__(self) -> None:
        self.plan: ChangePlan | None = None

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = SubmitPlanRequest.model_validate(request)
        if self.plan is not None:
            raise ValueError("plan already submitted")
        validate_change_plan(typed.plan)
        self.plan = typed.plan
        return SubmitPlanResult(
            accepted=True,
            changed_paths=tuple(file.path for file in typed.plan.files),
        )

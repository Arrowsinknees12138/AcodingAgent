"""Agent 执行边界的数据契约（实施设计第 9 节）。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import AgentRole
from repopilot.domain.errors import ErrorInfo


class AgentExecutionRequest(StrictModel):
    role: AgentRole
    work_item_id: UUID
    input_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    allowed_tools: tuple[str, ...]
    max_steps: int = Field(gt=0, le=20)
    remaining_model_calls: int = Field(gt=0)
    attempt: int = Field(default=1, gt=0)
    prompt_version: str = "1"
    tool_schema_version: str = "1"
    policy_version: str = "1"


class AgentExecutionResult(StrictModel):
    work_item_id: UUID
    status: Literal["succeeded", "failed"]
    output_refs: tuple[ArtifactRef, ...]
    model_call_ids: tuple[UUID, ...]
    error: ErrorInfo | None


class AgentTurn(StrictModel):
    """模型每轮只能返回一个结构化工具调用。"""

    tool: str = Field(min_length=1)
    arguments: dict[str, object]


class FinishRequest(StrictModel):
    status: Literal["succeeded", "failed"]
    output_refs: tuple[ArtifactRef, ...] = ()
    error: ErrorInfo | None = None

    @model_validator(mode="after")
    def _status_and_error_must_agree(self) -> FinishRequest:
        if self.status == "succeeded" and self.error is not None:
            raise ValueError("成功的 finish 不能携带 error")
        if self.status == "failed" and self.error is None:
            raise ValueError("失败的 finish 必须携带 error")
        return self

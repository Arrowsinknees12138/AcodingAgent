"""JSON 传输模型。

领域模型保持 strict；HTTP JSON 天然把 tuple 表示为 array、Decimal 表示为
number，因此先用传输模型解析，再通过 `model_validate_json` 进入严格领域边界。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from repopilot.domain.enums import ApprovalMode, ApprovalPolicyMode, RiskLevel
from repopilot.domain.policies import OFFICIAL_PYPI_INDEX, QaCheck
from repopilot.domain.tasks import (
    MAX_COST_USD,
    MAX_MODEL_CALLS,
    MAX_SANDBOX_SECONDS,
    MAX_WALL_TIME_SECONDS,
    CreateRunRequest,
)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryBody(ApiModel):
    url: str
    revision: str | None = None


class BudgetBody(ApiModel):
    max_cost_usd: Decimal = Field(gt=0, le=MAX_COST_USD)
    max_wall_time_seconds: int = Field(gt=0, le=MAX_WALL_TIME_SECONDS)
    max_model_calls: int = Field(gt=0, le=MAX_MODEL_CALLS)
    max_sandbox_seconds: int = Field(gt=0, le=MAX_SANDBOX_SECONDS)


class ApprovalStageBody(ApiModel):
    plan: ApprovalMode = ApprovalMode.AUTOMATIC
    execution: ApprovalMode = ApprovalMode.AUTOMATIC
    delivery: ApprovalMode = ApprovalMode.AUTOMATIC


class ApprovalPolicyBody(ApiModel):
    mode: ApprovalPolicyMode = ApprovalPolicyMode.RISK_BASED
    custom: ApprovalStageBody | None = None

    @model_validator(mode="after")
    def _custom_matches_mode(self) -> ApprovalPolicyBody:
        if self.mode is ApprovalPolicyMode.CUSTOM and self.custom is None:
            raise ValueError("custom 审批模式必须提供逐阶段设置")
        if self.mode is ApprovalPolicyMode.RISK_BASED and self.custom is not None:
            raise ValueError("risk_based 审批模式不能携带 custom 设置")
        return self


class DependencyPolicyBody(ApiModel):
    index_url: Literal["https://pypi.org/simple"] = OFFICIAL_PYPI_INDEX
    allow_lockfile_read: bool = True
    allow_cache: bool = True
    allow_new_dependencies: bool = False


def _standard_qa_checks() -> list[QaCheck]:
    return ["acceptance_tests", "targeted_tests", "scope_check"]


class QaPolicyBody(ApiModel):
    level: Literal["standard"] = "standard"
    required: list[QaCheck] = Field(default_factory=_standard_qa_checks)


class CreateRunBody(ApiModel):
    repository: RepositoryBody
    requirement: str = Field(min_length=1, max_length=200_000)
    acceptance_criteria: list[str] | None = None
    budget: BudgetBody
    approval_policy: ApprovalPolicyBody = Field(default_factory=ApprovalPolicyBody)
    dependency_policy: DependencyPolicyBody = Field(default_factory=DependencyPolicyBody)
    qa: QaPolicyBody = Field(default_factory=QaPolicyBody)
    risk_level: RiskLevel = RiskLevel.LOW

    def to_domain(self) -> CreateRunRequest:
        return CreateRunRequest.model_validate_json(self.model_dump_json())

"""Run/Task 顶层契约（实施设计第 7.1、27 节）。

校验约束（第 7.1 节）里"仓库工作树大小/文件数量"等需要真正 clone 之后
才能检查的规则，不在这里实现——它们属于 Milestone 4 的 Repository Service
职责。这里只做能在请求到达时就判断的静态校验。
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import Field, field_validator

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import RiskLevel, RunStatus
from repopilot.domain.errors import ErrorInfo
from repopilot.domain.policies import ApprovalPolicy, DependencyPolicy, QaPolicy

_GITHUB_URL_RE = re.compile(r"^https://github\.com/[\w.-]+/[\w.-]+$")
_FULL_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Phase 0 服务端硬上限（第 7.1 节）；客户端可以申请更低的预算，但不能超过它们。
MAX_COST_USD = Decimal("20")
MAX_WALL_TIME_SECONDS = 7_200
MAX_MODEL_CALLS = 100
MAX_SANDBOX_SECONDS = 3_600


class RepositoryInput(StrictModel):
    url: str
    revision: str | None = None

    @field_validator("url")
    @classmethod
    def _url_must_be_public_github_repo(cls, value: str) -> str:
        if not _GITHUB_URL_RE.match(value):
            raise ValueError("只接受 https://github.com/{owner}/{repo} 形式的公共仓库地址")
        return value


class BudgetInput(StrictModel):
    max_cost_usd: Decimal = Field(gt=0, le=MAX_COST_USD)
    max_wall_time_seconds: int = Field(gt=0, le=MAX_WALL_TIME_SECONDS)
    max_model_calls: int = Field(gt=0, le=MAX_MODEL_CALLS)
    max_sandbox_seconds: int = Field(gt=0, le=MAX_SANDBOX_SECONDS)


class CreateRunRequest(StrictModel):
    repository: RepositoryInput
    requirement: str = Field(min_length=1, max_length=200_000)
    acceptance_criteria: tuple[str, ...] | None = None
    budget: BudgetInput
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    dependency_policy: DependencyPolicy = DependencyPolicy()
    qa: QaPolicy = QaPolicy()
    # 在 Planner Activity 接入前由调用方提供；接入后将由 ChangePlan.risk_flags 推导。
    risk_level: RiskLevel = RiskLevel.LOW

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_acceptance_criteria(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return value
        if len(value) > 100:
            raise ValueError("acceptance_criteria 最多 100 条")
        for criterion in value:
            if not (1 <= len(criterion) <= 2_000):
                raise ValueError("每条 acceptance criterion 长度必须为 1～2000 字符")
        return value


class TaskSpec(StrictModel):
    task_id: UUID
    run_id: UUID
    tenant_id: UUID
    repository_url: str
    base_revision: str
    requirement: str
    acceptance_criteria: tuple[str, ...]
    acceptance_criteria_source: Literal["structured", "heuristic", "approved"]
    policy_profile: str
    budget: BudgetInput
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    dependency_policy: DependencyPolicy = DependencyPolicy()
    qa: QaPolicy = QaPolicy()
    risk_level: RiskLevel = RiskLevel.LOW

    @field_validator("base_revision")
    @classmethod
    def _base_revision_must_be_full_sha(cls, value: str) -> str:
        if not _FULL_COMMIT_SHA_RE.match(value):
            raise ValueError("base_revision 必须是完整 40 位 commit SHA，不能是分支名")
        return value

    @field_validator("acceptance_criteria")
    @classmethod
    def _acceptance_criteria_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) == 0:
            raise ValueError("TaskSpec 一旦创建，acceptance_criteria 不能为空")
        return value


class IngestResult(StrictModel):
    repository_url: str
    base_revision: str
    proposed_acceptance_criteria: tuple[str, ...]
    acceptance_criteria_source: Literal["structured", "heuristic", "missing"]
    requires_requirements_approval: bool
    task_spec_ref: ArtifactRef | None


class FinalReport(StrictModel):
    run_id: UUID
    status: RunStatus
    repository_url: str
    base_revision: str
    final_revision: str | None
    changed_paths: tuple[str, ...]
    patch_ref: ArtifactRef | None
    verification_ref: ArtifactRef | None
    review_ref: ArtifactRef | None
    cleanup_report_ref: ArtifactRef | None
    warnings: tuple[str, ...]
    model_calls: int
    input_tokens: int
    output_tokens: int
    model_cost_usd: Decimal
    sandbox_seconds: int
    duration_seconds: int
    repair_rounds: int
    error: ErrorInfo | None

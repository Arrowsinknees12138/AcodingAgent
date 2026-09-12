"""Policy Engine 的输入/输出契约（实施设计第 16 节）。

Policy Engine 本身必须是纯函数（输入 -> 输出，不做 I/O）；具体的
`authorize()` 实现和 YAML 规则加载放在 `services/policy_engine.py`，
这里只固定它的输入输出数据结构，保持 domain 层不依赖任何框架。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from repopilot.domain import StrictModel
from repopilot.domain.enums import ApprovalMode, ApprovalPolicyMode, RiskFlag, RiskLevel

OfficialPypiIndex = Literal["https://pypi.org/simple"]
QaCheck = Literal["acceptance_tests", "targeted_tests", "scope_check"]

OFFICIAL_PYPI_INDEX: OfficialPypiIndex = "https://pypi.org/simple"
STANDARD_QA_CHECKS: tuple[QaCheck, ...] = (
    "acceptance_tests",
    "targeted_tests",
    "scope_check",
)


class ApprovalStagePolicy(StrictModel):
    plan: ApprovalMode = ApprovalMode.AUTOMATIC
    execution: ApprovalMode = ApprovalMode.AUTOMATIC
    delivery: ApprovalMode = ApprovalMode.AUTOMATIC


class ApprovalPolicy(StrictModel):
    mode: ApprovalPolicyMode = ApprovalPolicyMode.RISK_BASED
    custom: ApprovalStagePolicy | None = None

    @model_validator(mode="after")
    def _custom_settings_match_mode(self) -> ApprovalPolicy:
        if self.mode is ApprovalPolicyMode.CUSTOM and self.custom is None:
            raise ValueError("custom 审批模式必须提供逐阶段设置")
        if self.mode is ApprovalPolicyMode.RISK_BASED and self.custom is not None:
            raise ValueError("risk_based 审批模式不能携带 custom 设置")
        return self


class ResolvedApprovalPolicy(StrictModel):
    risk_level: RiskLevel
    stages: ApprovalStagePolicy
    source: Literal["risk_based", "custom"]


class DependencyPolicy(StrictModel):
    index_url: OfficialPypiIndex = OFFICIAL_PYPI_INDEX
    allow_lockfile_read: bool = True
    allow_cache: bool = True
    allow_new_dependencies: bool = False

    @model_validator(mode="after")
    def _fixed_security_baseline(self) -> DependencyPolicy:
        if not self.allow_lockfile_read:
            raise ValueError("Phase 0 必须允许读取 lockfile")
        if not self.allow_cache:
            raise ValueError("Phase 0 必须允许依赖缓存")
        return self


class QaPolicy(StrictModel):
    level: Literal["standard"] = "standard"
    required: tuple[QaCheck, ...] = Field(default=STANDARD_QA_CHECKS, min_length=3)

    @model_validator(mode="after")
    def _standard_checks_are_complete(self) -> QaPolicy:
        if len(set(self.required)) != len(self.required):
            raise ValueError("QA required 不能包含重复检查")
        missing = set(STANDARD_QA_CHECKS).difference(self.required)
        if missing:
            raise ValueError(f"standard QA 缺少必需检查: {', '.join(sorted(missing))}")
        return self


_HIGH_RISK_FLAGS = frozenset(
    {RiskFlag.AUTH_CHANGE, RiskFlag.MIGRATION, RiskFlag.PROMPT_INJECTION_SUSPECTED}
)
_MEDIUM_RISK_FLAGS = frozenset(
    {
        RiskFlag.DEPENDENCY_CHANGE,
        RiskFlag.CI_CONFIG,
        RiskFlag.LARGE_SCOPE,
        RiskFlag.UNCLEAR_ACCEPTANCE_CRITERIA,
    }
)


def classify_risk(flags: tuple[RiskFlag, ...]) -> RiskLevel:
    flag_set = frozenset(flags)
    if flag_set.intersection(_HIGH_RISK_FLAGS):
        return RiskLevel.HIGH
    if flag_set.intersection(_MEDIUM_RISK_FLAGS):
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def resolve_approval_policy(
    risk_level: RiskLevel, policy: ApprovalPolicy
) -> ResolvedApprovalPolicy:
    if policy.mode is ApprovalPolicyMode.CUSTOM:
        assert policy.custom is not None
        return ResolvedApprovalPolicy(
            risk_level=risk_level,
            stages=policy.custom,
            source="custom",
        )

    stages = ApprovalStagePolicy()
    if risk_level is RiskLevel.MEDIUM:
        stages = ApprovalStagePolicy(plan=ApprovalMode.MANUAL)
    elif risk_level is RiskLevel.HIGH:
        stages = ApprovalStagePolicy(
            plan=ApprovalMode.MANUAL,
            execution=ApprovalMode.MANUAL,
        )
    return ResolvedApprovalPolicy(risk_level=risk_level, stages=stages, source="risk_based")


def dependency_manifest_paths(paths: tuple[str, ...]) -> tuple[str, ...]:
    """返回会改变 Python 依赖解析结果的受控清单文件。"""
    manifests: list[str] = []
    for path in paths:
        normalized = path.replace("\\", "/").removeprefix("./")
        name = normalized.rsplit("/", 1)[-1].lower()
        if name in {"pyproject.toml", "uv.lock", "setup.py", "setup.cfg"} or (
            name.startswith("requirements") and name.endswith((".txt", ".in"))
        ):
            manifests.append(normalized)
    return tuple(sorted(set(manifests)))


class PolicyContext(StrictModel):
    tenant_id: UUID
    run_id: UUID
    work_item_id: UUID | None
    policy_version: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]


class PolicyAction(StrictModel):
    kind: Literal["read", "write", "execute", "artifact_read"]
    resource: str
    arguments: tuple[str, ...] = ()


class PolicyDecision(StrictModel):
    allowed: bool
    reason_code: str
    normalized_resource: str

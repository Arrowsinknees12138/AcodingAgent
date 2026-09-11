"""Approval Update 的契约与校验规则（实施设计第 8.7 节）。

Temporal Update 的 handler 本身写在 `workflows/code_repair.py`；这里只放
纯函数校验逻辑，方便脱离 Temporal 运行时单独做单元测试（Workflow 代码
在真实 Temporal 环境之外很难细粒度单测每一条分支）。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from repopilot.domain import StrictModel
from repopilot.domain.enums import RunStatus

ApprovalKind = Literal["requirements", "plan", "test", "delivery"]


class ApprovalRequest(StrictModel):
    approval_id: UUID
    kind: ApprovalKind
    decision: Literal["approve", "reject"]
    actor_id: str
    reason: str
    replacement_acceptance_criteria: tuple[str, ...] | None = None


# 每种 approval kind 只能在对应的等待状态下提交，防止客户端把过期/错配的
# Update 应用到当前 Run（例如计划已经过了 PLANNING 阶段却还收到一个
# "plan" approval）。
_EXPECTED_STATUS_BY_KIND: dict[ApprovalKind, RunStatus] = {
    "requirements": RunStatus.WAITING_REQUIREMENTS_APPROVAL,
    "plan": RunStatus.WAITING_PLAN_APPROVAL,
    "test": RunStatus.WAITING_TEST_APPROVAL,
    "delivery": RunStatus.WAITING_DELIVERY_APPROVAL,
}


class ApprovalRejected(ValueError):
    """Update 校验失败，Workflow 应当拒绝这次 Update 而不是应用它。"""


def validate_approval(
    request: ApprovalRequest,
    *,
    current_status: RunStatus,
    already_processed_ids: frozenset[UUID],
) -> None:
    """校验 Approval Update；只做校验，不产生任何副作用（第 6 节：workflows 不做 I/O）。"""
    if request.approval_id in already_processed_ids:
        raise ApprovalRejected(f"approval_id={request.approval_id} 已经处理过，拒绝重复应用")

    expected_status = _EXPECTED_STATUS_BY_KIND[request.kind]
    if current_status is not expected_status:
        raise ApprovalRejected(
            f"当前状态 {current_status.value} 与 approval kind={request.kind} 不匹配"
            f"（需要 {expected_status.value}）"
        )

    if request.decision == "reject" and not request.reason.strip():
        raise ApprovalRejected("reject 必须提供非空 reason")

    if (
        request.kind == "requirements"
        and request.decision == "approve"
        and not request.replacement_acceptance_criteria
    ):
        raise ApprovalRejected("requirements approval 必须提供非空验收条件")

    if request.kind == "test" and request.decision == "approve" and not request.reason.strip():
        raise ApprovalRejected("test waiver 必须记录被豁免测试和原因（写在 reason 字段）")

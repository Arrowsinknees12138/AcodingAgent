"""Approval Update 校验规则（实施设计 8.7 节）。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from repopilot.domain.enums import RunStatus
from repopilot.workflows.updates import ApprovalRejected, ApprovalRequest, validate_approval


def test_approval_must_match_current_status() -> None:
    request = ApprovalRequest(
        approval_id=uuid4(), kind="plan", decision="approve", actor_id="u1", reason="looks good"
    )
    with pytest.raises(ApprovalRejected):
        validate_approval(
            request,
            current_status=RunStatus.WAITING_TEST_APPROVAL,
            already_processed_ids=frozenset(),
        )


def test_duplicate_approval_id_rejected() -> None:
    approval_id = uuid4()
    request = ApprovalRequest(
        approval_id=approval_id,
        kind="delivery",
        decision="approve",
        actor_id="u1",
        reason="ok",
    )
    with pytest.raises(ApprovalRejected):
        validate_approval(
            request,
            current_status=RunStatus.WAITING_DELIVERY_APPROVAL,
            already_processed_ids=frozenset({approval_id}),
        )


def test_reject_requires_reason() -> None:
    request = ApprovalRequest(
        approval_id=uuid4(), kind="delivery", decision="reject", actor_id="u1", reason=""
    )
    with pytest.raises(ApprovalRejected):
        validate_approval(
            request,
            current_status=RunStatus.WAITING_DELIVERY_APPROVAL,
            already_processed_ids=frozenset(),
        )


def test_requirements_approval_requires_replacement_criteria() -> None:
    request = ApprovalRequest(
        approval_id=uuid4(),
        kind="requirements",
        decision="approve",
        actor_id="u1",
        reason="provided below",
        replacement_acceptance_criteria=None,
    )
    with pytest.raises(ApprovalRejected):
        validate_approval(
            request,
            current_status=RunStatus.WAITING_REQUIREMENTS_APPROVAL,
            already_processed_ids=frozenset(),
        )


def test_valid_delivery_approval_passes() -> None:
    request = ApprovalRequest(
        approval_id=uuid4(), kind="delivery", decision="approve", actor_id="u1", reason="reviewed"
    )
    validate_approval(
        request,
        current_status=RunStatus.WAITING_DELIVERY_APPROVAL,
        already_processed_ids=frozenset(),
    )


def test_execution_approval_matches_execution_wait_state() -> None:
    request = ApprovalRequest(
        approval_id=uuid4(),
        kind="execution",
        decision="approve",
        actor_id="u1",
        reason="approved after reviewing the test design",
    )
    validate_approval(
        request,
        current_status=RunStatus.WAITING_EXECUTION_APPROVAL,
        already_processed_ids=frozenset(),
    )

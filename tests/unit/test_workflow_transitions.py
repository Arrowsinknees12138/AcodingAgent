"""Run 状态机合法/非法转换（实施设计 8.6/8.10、23.1 节）。"""

from __future__ import annotations

import pytest

from repopilot.domain.enums import RunStatus
from repopilot.workflows.transitions import InvalidRunStatusTransition, validate_transition


def test_happy_path_auto_approved_run() -> None:
    for current, new in [
        (RunStatus.QUEUED, RunStatus.INGESTING),
        (RunStatus.INGESTING, RunStatus.BASELINING),
        (RunStatus.BASELINING, RunStatus.PLANNING),
        (RunStatus.PLANNING, RunStatus.DESIGNING_TESTS),
        (RunStatus.DESIGNING_TESTS, RunStatus.EXECUTING),
        (RunStatus.EXECUTING, RunStatus.VERIFYING),
        (RunStatus.VERIFYING, RunStatus.REVIEWING),
        (RunStatus.REVIEWING, RunStatus.WAITING_DELIVERY_APPROVAL),
        (RunStatus.WAITING_DELIVERY_APPROVAL, RunStatus.FINALIZING),
        (RunStatus.FINALIZING, RunStatus.SUCCEEDED),
    ]:
        validate_transition(current, new)  # 不抛异常即通过


def test_any_non_terminal_status_can_escalate_to_finalizing() -> None:
    for status in RunStatus:
        if status in {RunStatus.FINALIZING} or status in (
            RunStatus.SUCCEEDED,
            RunStatus.REJECTED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        ):
            continue
        validate_transition(status, RunStatus.FINALIZING)


def test_finalizing_only_reaches_terminal_statuses() -> None:
    validate_transition(RunStatus.FINALIZING, RunStatus.FAILED)
    with pytest.raises(InvalidRunStatusTransition):
        validate_transition(RunStatus.FINALIZING, RunStatus.PLANNING)


def test_terminal_statuses_are_final() -> None:
    for terminal in (
        RunStatus.SUCCEEDED,
        RunStatus.REJECTED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    ):
        with pytest.raises(InvalidRunStatusTransition):
            validate_transition(terminal, RunStatus.INGESTING)


def test_skipping_directly_from_ingesting_to_planning_is_illegal() -> None:
    with pytest.raises(InvalidRunStatusTransition):
        validate_transition(RunStatus.INGESTING, RunStatus.PLANNING)

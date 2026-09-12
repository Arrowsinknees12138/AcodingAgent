"""Run 状态机的合法转换表（实施设计第 8.6、8.10 节）。

`workflows` 层不允许有任何外部 I/O（第 6 节），这个模块只做一件事：给定
当前状态和目标状态，判断这次转换是否合法。真正驱动状态变化的是
`workflows/code_repair.py` 里的 `CodeRepairWorkflow`。

设计要点（对应 8.10 节末尾的规则）：`FAILED`/`REJECTED`/`CANCELLED` 都是
`finalize(outcome)` 的简写，必须先经过 `FINALIZING`（执行 cleanup、生成
Final Report），不能从任何中间状态直接跳到终态；`FINALIZING` 因此是唯一
能到达终态的"闸口"，而几乎任何非终态都可能因为审批拒绝/校验失败/取消
请求而提前进入 `FINALIZING`。
"""

from __future__ import annotations

from repopilot.domain.enums import TERMINAL_RUN_STATUSES, RunStatus

# 正常推进路径的合法边（不含"任意状态 -> FINALIZING"和
# "FINALIZING -> 终态"，那两类在 validate_transition 里单独处理）。
_FORWARD_EDGES: frozenset[tuple[RunStatus, RunStatus]] = frozenset(
    {
        (RunStatus.QUEUED, RunStatus.INGESTING),
        (RunStatus.INGESTING, RunStatus.WAITING_REQUIREMENTS_APPROVAL),
        (RunStatus.INGESTING, RunStatus.BASELINING),
        (RunStatus.WAITING_REQUIREMENTS_APPROVAL, RunStatus.BASELINING),
        (RunStatus.BASELINING, RunStatus.WAITING_TEST_APPROVAL),
        (RunStatus.BASELINING, RunStatus.PLANNING),
        (RunStatus.WAITING_TEST_APPROVAL, RunStatus.PLANNING),
        (RunStatus.WAITING_TEST_APPROVAL, RunStatus.EXECUTING),
        (RunStatus.PLANNING, RunStatus.WAITING_PLAN_APPROVAL),
        (RunStatus.PLANNING, RunStatus.DESIGNING_TESTS),
        (RunStatus.WAITING_PLAN_APPROVAL, RunStatus.DESIGNING_TESTS),
        (RunStatus.DESIGNING_TESTS, RunStatus.WAITING_EXECUTION_APPROVAL),
        (RunStatus.WAITING_EXECUTION_APPROVAL, RunStatus.EXECUTING),
        (RunStatus.DESIGNING_TESTS, RunStatus.WAITING_TEST_APPROVAL),
        (RunStatus.DESIGNING_TESTS, RunStatus.EXECUTING),
        (RunStatus.EXECUTING, RunStatus.VERIFYING),
        (RunStatus.EXECUTING, RunStatus.REPLANNING),
        (RunStatus.REPLANNING, RunStatus.PLANNING),
        (RunStatus.VERIFYING, RunStatus.REVIEWING),
        (RunStatus.VERIFYING, RunStatus.EXECUTING),  # 验证失败触发的 repair round
        (RunStatus.REVIEWING, RunStatus.WAITING_DELIVERY_APPROVAL),
        (RunStatus.REVIEWING, RunStatus.EXECUTING),  # request_changes 触发的 repair round
        (RunStatus.WAITING_DELIVERY_APPROVAL, RunStatus.FINALIZING),
    }
)

_FINALIZING_TARGETS: frozenset[RunStatus] = frozenset(
    {RunStatus.SUCCEEDED, RunStatus.REJECTED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class InvalidRunStatusTransition(ValueError):
    def __init__(self, current: RunStatus, new: RunStatus) -> None:
        super().__init__(f"不允许从 {current.value} 转换到 {new.value}")
        self.current = current
        self.new = new


def validate_transition(current: RunStatus, new: RunStatus) -> None:
    """校验 `current -> new` 是否合法，不合法则抛 `InvalidRunStatusTransition`。"""
    if current in TERMINAL_RUN_STATUSES:
        raise InvalidRunStatusTransition(current, new)

    if new is RunStatus.FINALIZING:
        if current is RunStatus.FINALIZING:
            raise InvalidRunStatusTransition(current, new)
        return  # 任何非终态、非 FINALIZING 的状态都可以提前进入 FINALIZING。

    if current is RunStatus.FINALIZING:
        if new in _FINALIZING_TARGETS:
            return
        raise InvalidRunStatusTransition(current, new)

    if (current, new) in _FORWARD_EDGES:
        return

    raise InvalidRunStatusTransition(current, new)

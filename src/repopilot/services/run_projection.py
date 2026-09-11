"""Run 状态查询投影的服务协议（实施设计第 17.1、17.2 节）。

`run_projections` 是 Temporal 状态的只读查询副本，不是 Workflow 的恢复
来源；这里定义的 `apply()` 必须是幂等的——同一个投影事件（相同 run_id +
相同字段取值）被 Temporal Activity 重试多次，效果等同于只应用一次。
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from repopilot.domain import StrictModel
from repopilot.domain.enums import TERMINAL_RUN_STATUSES, RunStatus


class ProjectionEvent(StrictModel):
    run_id: UUID
    tenant_id: UUID
    workflow_id: str
    status: RunStatus
    base_revision: str | None = None
    plan_version: int = 0
    model_calls: int = 0
    occurred_at: datetime


class RunProjectionStore(Protocol):
    async def apply(self, event: ProjectionEvent) -> None: ...

    async def get(self, run_id: UUID) -> ProjectionEvent | None: ...


def is_terminal(status: RunStatus) -> bool:
    return status in TERMINAL_RUN_STATUSES

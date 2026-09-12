"""API 使用的 Run 控制面协议。"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from repopilot.domain import StrictModel
from repopilot.domain.enums import RunStatus
from repopilot.domain.tasks import CreateRunRequest
from repopilot.workflows.updates import ApprovalRequest


class RunNotFoundError(LookupError):
    pass


class IdempotencyConflictError(ValueError):
    pass


class RunUnavailableError(RuntimeError):
    pass


class RunView(StrictModel):
    run_id: UUID
    workflow_id: str
    status: RunStatus
    base_revision: str | None = None
    plan_version: int = 0
    model_calls: int = 0


class RunControl(Protocol):
    async def create(self, request: CreateRunRequest, *, idempotency_key: str) -> RunView: ...

    async def get(self, run_id: UUID) -> RunView: ...

    async def approve(self, run_id: UUID, approval: ApprovalRequest) -> None: ...

    async def cancel(self, run_id: UUID) -> None: ...

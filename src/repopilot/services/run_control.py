"""API 使用的 Run 控制面协议。"""

from __future__ import annotations

from datetime import datetime
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


class RunEventView(StrictModel):
    event_id: UUID
    event_type: str
    actor_type: str
    actor_id: str | None
    payload: dict[str, object]
    created_at: datetime


class ArtifactView(StrictModel):
    artifact_id: UUID
    run_id: UUID
    kind: str
    schema_version: str
    sha256: str
    size_bytes: int
    base_revision: str | None
    created_at: datetime


class ArtifactDownload(StrictModel):
    artifact: ArtifactView
    content: bytes


class RunControl(Protocol):
    async def create(self, request: CreateRunRequest, *, idempotency_key: str) -> RunView: ...

    async def get(self, run_id: UUID) -> RunView: ...

    async def approve(self, run_id: UUID, approval: ApprovalRequest) -> None: ...

    async def cancel(self, run_id: UUID) -> None: ...

    async def list_events(self, run_id: UUID) -> tuple[RunEventView, ...]: ...

    async def list_artifacts(self, run_id: UUID) -> tuple[ArtifactView, ...]: ...

    async def download_artifact(self, artifact_id: UUID) -> ArtifactDownload: ...

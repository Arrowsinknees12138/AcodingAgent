"""Run 创建、查询、审批和取消路由。"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from repopilot.api.dependencies import authenticate, get_run_control
from repopilot.api.schemas import CreateRunBody
from repopilot.services.run_control import (
    ArtifactView,
    IdempotencyConflictError,
    RunControl,
    RunEventView,
    RunNotFoundError,
    RunUnavailableError,
    RunView,
)
from repopilot.workflows.updates import ApprovalKind, ApprovalRequest

router = APIRouter(prefix="/v1/runs", tags=["runs"])
Authenticated = Annotated[UUID, Depends(authenticate)]
Control = Annotated[RunControl, Depends(get_run_control)]


class ApprovalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ApprovalKind
    decision: Literal["approve", "reject"]
    actor_id: str = Field(min_length=1, max_length=200)
    reason: str = Field(max_length=4_000)
    replacement_acceptance_criteria: tuple[str, ...] | None = None


@router.post("", response_model=RunView, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    body: CreateRunBody,
    control: Control,
    _tenant_id: Authenticated,
    idempotency_key: Annotated[UUID, Header(alias="Idempotency-Key")],
) -> RunView:
    try:
        request = body.to_domain()
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc
    try:
        return await control.create(request, idempotency_key=str(idempotency_key))
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RunUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.get("/{run_id}", response_model=RunView)
async def get_run(run_id: UUID, control: Control, _tenant_id: Authenticated) -> RunView:
    try:
        return await control.get(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{run_id}/events", response_model=list[RunEventView])
async def list_events(
    run_id: UUID, control: Control, _tenant_id: Authenticated
) -> tuple[RunEventView, ...]:
    try:
        return await control.list_events(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.get("/{run_id}/artifacts", response_model=list[ArtifactView])
async def list_artifacts(
    run_id: UUID, control: Control, _tenant_id: Authenticated
) -> tuple[ArtifactView, ...]:
    try:
        return await control.list_artifacts(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/{run_id}/approvals", status_code=status.HTTP_204_NO_CONTENT)
async def submit_approval(
    run_id: UUID,
    body: ApprovalBody,
    control: Control,
    _tenant_id: Authenticated,
) -> None:
    try:
        await control.approve(
            run_id,
            ApprovalRequest(
                approval_id=uuid4(),
                kind=body.kind,
                decision=body.decision,
                actor_id=body.actor_id,
                reason=body.reason,
                replacement_acceptance_criteria=body.replacement_acceptance_criteria,
            ),
        )
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RunUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{run_id}:cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_run(run_id: UUID, control: Control, _tenant_id: Authenticated) -> None:
    try:
        await control.cancel(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RunUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

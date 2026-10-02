"""Manual Actions import. Repair requires reproducible evidence, never a bug prompt."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from repopilot.api.dependencies import authenticate, get_run_control
from repopilot.api.schemas import ApiModel, ApprovalPolicyBody, BudgetBody
from repopilot.domain.ci import CiDiagnosisReport, CiImportSpec, parse_run_url
from repopilot.domain.tasks import CreateRunRequest, FinalReport
from repopilot.services.ci_source import CiRun, CiSourceError, CiSourceReader
from repopilot.services.run_control import (
    IdempotencyConflictError,
    RunControl,
    RunNotFoundError,
    RunUnavailableError,
    RunView,
)

router = APIRouter(prefix="/v1/ci", tags=["ci"])
Auth = Annotated[UUID, Depends(authenticate)]
Control = Annotated[RunControl, Depends(get_run_control)]


def get_ci_reader(request: Request) -> CiSourceReader:
    reader = getattr(request.app.state, "ci_reader", None)
    if reader is None:
        raise HTTPException(503, "CI reader is not configured")
    return cast(CiSourceReader, reader)


Reader = Annotated[CiSourceReader, Depends(get_ci_reader)]


class CiImportBody(ApiModel):
    run_url: str = Field(max_length=500)
    job_id: int | None = Field(default=None, gt=0)
    mode: Literal["diagnose", "repair"] = "diagnose"
    budget: BudgetBody = Field(
        default_factory=lambda: BudgetBody(
            max_cost_usd=5, max_wall_time_seconds=3600, max_model_calls=50, max_sandbox_seconds=1800
        )
    )
    approval_policy: ApprovalPolicyBody = Field(default_factory=ApprovalPolicyBody)


class CiDetail(ApiModel):
    run: RunView
    diagnosis: CiDiagnosisReport | None = None
    final_report: FinalReport | None = None


async def inspect(reader: CiSourceReader, url: str) -> CiRun:
    try:
        parse_run_url(url)
        return await reader.inspect(url)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except CiSourceError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/jobs", response_model=CiRun)
async def jobs(
    reader: Reader, _auth: Auth, run_url: Annotated[str, Query(max_length=500)]
) -> CiRun:
    return await inspect(reader, run_url)


@router.post("/import", response_model=RunView, status_code=202)
async def import_run(body: CiImportBody, reader: Reader, control: Control, _auth: Auth) -> RunView:
    run = await inspect(reader, body.run_url)
    if run.status != "completed" or run.conclusion != "failure":
        raise HTTPException(422, "Select a completed failed run attempt")
    failed = [
        j
        for j in run.jobs
        if j.conclusion == "failure" and (body.job_id is None or body.job_id == j.job_id)
    ]
    if len(failed) != 1:
        raise HTTPException(422, "Select one failed job; use /v1/ci/jobs to list candidates")
    url = run.canonical_url()
    spec = CiImportSpec(run_url=url, job_id=failed[0].job_id, mode=body.mode)
    request = CreateRunRequest.model_validate_json(
        CiImportBodyRequest(
            repository={"url": run.repository_url, "revision": run.head_sha},
            requirement=f"Process CI failure from {url}, job {spec.job_id}",
            acceptance_criteria=["Collect evidence and apply the CI reproduction gate"],
            budget=body.budget,
            approval_policy=body.approval_policy,
            ci_import=spec.model_dump(mode="json"),
        ).model_dump_json()
    )
    identity = f"ci:{run.repository_id}:{run.run_id}:{run.run_attempt}:{spec.job_id}:{spec.mode}"
    try:
        return await control.create(
            request, idempotency_key=hashlib.sha256(identity.encode()).hexdigest()
        )
    except IdempotencyConflictError as exc:
        raise HTTPException(
            409, "This CI import already exists with different budget or approval settings"
        ) from exc
    except RunUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc


class CiImportBodyRequest(ApiModel):
    repository: dict[str, str]
    requirement: str
    acceptance_criteria: list[str]
    budget: BudgetBody
    approval_policy: ApprovalPolicyBody
    ci_import: dict[str, object]


@router.get("/{run_id}", response_model=CiDetail)
async def detail(run_id: UUID, control: Control, _auth: Auth) -> CiDetail:
    try:
        view = await control.get(run_id)
        artifacts = await control.list_artifacts(run_id)
        result = CiDetail(run=view)
        for artifact in artifacts:
            if artifact.kind not in {"ci_diagnosis", "final_report"}:
                continue
            content = (await control.download_artifact(artifact.artifact_id)).content
            if artifact.kind == "ci_diagnosis":
                result.diagnosis = CiDiagnosisReport.model_validate_json(content)
            else:
                result.final_report = FinalReport.model_validate_json(content)
        return result
    except RunNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{run_id}:repair", response_model=RunView, status_code=202)
async def repair(run_id: UUID, reader: Reader, control: Control, auth: Auth) -> RunView:
    result = await detail(run_id, control, auth)
    if (
        result.diagnosis is None
        or not result.diagnosis.repair_eligible
        or result.diagnosis.source is None
    ):
        raise HTTPException(409, "A reproducible code-failure diagnosis is required")
    if result.run.status.value not in {"succeeded", "rejected"}:
        raise HTTPException(409, "Wait for diagnosis completion before requesting repair")
    originals = await control.list_artifacts(run_id)
    original = next((a for a in originals if a.kind == "create_run_request"), None)
    if original is None:
        raise HTTPException(409, "Original import settings unavailable")
    saved = CreateRunRequest.model_validate_json(
        (await control.download_artifact(original.artifact_id)).content
    )
    source = result.diagnosis.source
    body = CiImportBody.model_validate_json(
        json.dumps(
            {
                "run_url": source.run_url,
                "job_id": source.job_id,
                "mode": "repair",
                "budget": saved.budget.model_dump(mode="json"),
                "approval_policy": saved.approval_policy.model_dump(mode="json"),
            }
        )
    )
    return await import_run(body, reader, control, auth)

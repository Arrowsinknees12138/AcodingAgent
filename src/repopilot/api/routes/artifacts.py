"""Artifact 下载路由。"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status

from repopilot.api.dependencies import authenticate, get_run_control
from repopilot.services.run_control import RunControl, RunNotFoundError

router = APIRouter(prefix="/v1/artifacts", tags=["artifacts"])


@router.get("/{artifact_id}/download")
async def download_artifact(
    artifact_id: UUID,
    control: Annotated[RunControl, Depends(get_run_control)],
    _tenant_id: Annotated[UUID, Depends(authenticate)],
) -> Response:
    try:
        download = await control.download_artifact(artifact_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(
        content=download.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{download.artifact.artifact_id}.bin"',
            "ETag": download.artifact.sha256,
        },
    )

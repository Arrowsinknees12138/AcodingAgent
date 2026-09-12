"""FastAPI 依赖：认证与 Run 控制面。"""

from __future__ import annotations

import secrets
from typing import Annotated, cast
from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from repopilot.config import Settings
from repopilot.services.run_control import RunControl

_bearer = HTTPBearer(auto_error=False)


def get_run_control(request: Request) -> RunControl:
    control = getattr(request.app.state, "run_control", None)
    if control is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="not ready")
    return cast(RunControl, control)


def authenticate(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> UUID:
    settings = cast(Settings, request.app.state.settings)
    expected = settings.api_token.get_secret_value()
    if credentials is None or not secrets.compare_digest(credentials.credentials, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    return settings.local_tenant_id

"""健康检查路由：/health/live 与 /health/ready（设计文档 19.1 节）。

- live：进程本身活着，不检查任何外部依赖，用于容器/进程存活探针。
- ready：额外检查关键外部依赖（当前阶段：数据库）是否可用，
  用于判断是否应该接收新的 Run 请求。
"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request, Response, status

from repopilot.services.readiness import ReadinessProbe

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request, response: Response) -> dict[str, str]:
    probe = cast(ReadinessProbe, request.app.state.readiness_probe)
    is_ready = await probe.check()
    response.status_code = status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ok" if is_ready else "unavailable"}

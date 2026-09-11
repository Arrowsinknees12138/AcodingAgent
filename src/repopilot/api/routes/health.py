"""健康检查路由：/health/live 与 /health/ready（设计文档 19.1 节）。

- live：进程本身活着，不检查任何外部依赖，用于容器/进程存活探针。
- ready：额外检查关键外部依赖（当前阶段：数据库）是否可用，
  用于判断是否应该接收新的 Run 请求。
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

router = APIRouter(tags=["health"])


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(response: Response) -> dict[str, str]:
    # Milestone 1 阶段尚未接入数据库连接池，先返回 ok；
    # Milestone 2 引入 SQLAlchemy engine 后在此处补充真实的连通性检查。
    response.status_code = status.HTTP_200_OK
    return {"status": "ok"}

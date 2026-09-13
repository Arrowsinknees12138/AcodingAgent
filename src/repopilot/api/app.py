"""FastAPI 应用入口。

依赖方向约束（设计文档第 6 节）：API 层只允许启动/查询/取消 Workflow，
不得直接调用 Agent 或 Sandbox；具体的 Run 相关路由在 Milestone 3
（Temporal 最小闭环）落地后加入 routes/runs.py。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse

from repopilot.api.routes import artifacts, health, runs
from repopilot.config import Settings, get_settings
from repopilot.infrastructure.artifacts.minio import MinioArtifactStore, build_minio_client
from repopilot.infrastructure.db.engine import get_session_factory
from repopilot.infrastructure.db.readiness import DatabaseReadinessProbe
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.run_control import PostgresTemporalRunControl
from repopilot.infrastructure.temporal.client import connect
from repopilot.logging import configure_logging, get_logger
from repopilot.services.readiness import ReadinessProbe
from repopilot.services.run_control import RunControl


def create_app(
    *,
    run_control: RunControl | None = None,
    settings: Settings | None = None,
    readiness_probe: ReadinessProbe | None = None,
) -> FastAPI:
    configure_logging()
    resolved_settings = settings or get_settings()
    logger = get_logger(component="api")
    logger.info("api.starting", **resolved_settings.safe_summary())

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if run_control is None:
            session_factory = get_session_factory()
            artifact_store = MinioArtifactStore(
                client=build_minio_client(
                    resolved_settings.minio_endpoint,
                    resolved_settings.minio_access_key,
                    resolved_settings.minio_secret_key.get_secret_value(),
                ),
                bucket=resolved_settings.minio_bucket,
                session_factory=session_factory,
            )
            await artifact_store.ensure_bucket()
            projection_store = PostgresRunProjectionStore(session_factory)
            application.state.run_control = PostgresTemporalRunControl(
                tenant_id=resolved_settings.local_tenant_id,
                session_factory=session_factory,
                artifact_store=artifact_store,
                projection_store=projection_store,
                temporal_client=await connect(resolved_settings),
            )
        yield

    app = FastAPI(title="RepoPilot API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved_settings
    app.state.readiness_probe = readiness_probe or DatabaseReadinessProbe(get_session_factory())
    if run_control is not None:
        app.state.run_control = run_control

    @app.get("/ui", include_in_schema=False)
    async def results_page() -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("results.html"),
            media_type="text/html",
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
                ),
            },
        )

    app.include_router(health.router)
    app.include_router(runs.router)
    app.include_router(artifacts.router)
    return app


app = create_app()


def run() -> None:
    """`repopilot-api` console script 入口。"""
    settings = get_settings()
    uvicorn.run(
        "repopilot.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.env == "dev",
    )


if __name__ == "__main__":
    run()

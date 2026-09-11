"""FastAPI 应用入口。

依赖方向约束（设计文档第 6 节）：API 层只允许启动/查询/取消 Workflow，
不得直接调用 Agent 或 Sandbox；具体的 Run 相关路由在 Milestone 3
（Temporal 最小闭环）落地后加入 routes/runs.py。
"""

from __future__ import annotations

import uvicorn
from fastapi import FastAPI

from repopilot.api.routes import health
from repopilot.config import get_settings
from repopilot.logging import configure_logging, get_logger


def create_app() -> FastAPI:
    configure_logging()
    settings = get_settings()
    logger = get_logger(component="api")
    logger.info("api.starting", **settings.safe_summary())

    app = FastAPI(title="RepoPilot API", version="0.1.0")
    app.include_router(health.router)
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

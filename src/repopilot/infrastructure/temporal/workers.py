"""Worker 启动器：`repopilot-worker <queue>`（实施设计第 4、8.2 节）。

同一份代码仓库按 Task Queue 启动成不同的 Worker 进程；每个进程只注册
自己 Task Queue 需要的 Workflow/Activity，也只加载自己需要的凭据——
例如 orchestration-worker 不持有模型或 Docker 凭据。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from temporalio.worker import Worker

from repopilot.activities.projections import ProjectionActivities
from repopilot.config import get_settings
from repopilot.infrastructure.db.engine import get_session_factory
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.temporal.client import ORCHESTRATION_TASK_QUEUE, connect
from repopilot.logging import configure_logging, get_logger
from repopilot.workflows.code_repair import CodeRepairWorkflow

_KNOWN_QUEUES = ("orchestration", "model", "repository", "sandbox")


async def _run_orchestration_worker() -> None:
    settings = get_settings()
    client = await connect(settings)
    projection_store = PostgresRunProjectionStore(get_session_factory())
    projection_activities = ProjectionActivities(projection_store)

    worker = Worker(
        client,
        task_queue=ORCHESTRATION_TASK_QUEUE,
        workflows=[CodeRepairWorkflow],
        activities=[projection_activities.update_projection],
    )
    logger = get_logger(component="worker", queue="orchestration")
    logger.info("worker.starting", task_queue=ORCHESTRATION_TASK_QUEUE)
    await worker.run()


async def _run(queue: str) -> None:
    if queue == "orchestration":
        await _run_orchestration_worker()
        return
    # model/repository/sandbox worker 分别在 Milestone 6（Model Gateway +
    # Agents）、Milestone 4（Repository Service）、Milestone 5（Sandbox）
    # 落地对应 Activity 之后才有内容可注册；提前起一个空 Worker 只会
    # 制造"看起来在运行但什么都不做"的假象。
    raise NotImplementedError(
        f"'{queue}' worker 还未实现，会在对应 Milestone 落地 Activity 之后接入"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="repopilot-worker")
    parser.add_argument("queue", choices=_KNOWN_QUEUES)
    args = parser.parse_args(sys.argv[1:])

    configure_logging()
    asyncio.run(_run(args.queue))


if __name__ == "__main__":
    main()

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

from repopilot.activities.ingest import IngestActivities
from repopilot.activities.projections import ProjectionActivities
from repopilot.activities.repository import RepositoryActivities
from repopilot.activities.verification import VerificationActivities
from repopilot.config import get_settings
from repopilot.infrastructure.artifacts.minio import MinioArtifactStore, build_minio_client
from repopilot.infrastructure.db.engine import get_session_factory
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.git.repository_service import GitRepositoryService
from repopilot.infrastructure.sandbox.docker import DockerSandboxService
from repopilot.infrastructure.temporal.client import connect
from repopilot.logging import configure_logging, get_logger
from repopilot.services.task_queues import (
    ORCHESTRATION_TASK_QUEUE,
    REPOSITORY_TASK_QUEUE,
    SANDBOX_TASK_QUEUE,
)
from repopilot.services.verification_service import BaselineVerificationService
from repopilot.workflows.code_repair import CodeRepairWorkflow

_KNOWN_QUEUES = ("orchestration", "model", "repository", "sandbox")


async def _artifact_store() -> MinioArtifactStore:
    settings = get_settings()
    store = MinioArtifactStore(
        client=build_minio_client(
            settings.minio_endpoint,
            settings.minio_access_key,
            settings.minio_secret_key.get_secret_value(),
        ),
        bucket=settings.minio_bucket,
        session_factory=get_session_factory(),
    )
    await store.ensure_bucket()
    return store


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


async def _run_repository_worker() -> None:
    settings = get_settings()
    client = await connect(settings)
    artifacts = await _artifact_store()
    repository = GitRepositoryService(
        data_dir=settings.data_dir,
        artifact_store=artifacts,
        tenant_id=settings.local_tenant_id,
    )
    ingest = IngestActivities(artifact_store=artifacts, repository=repository)
    repository_activities = RepositoryActivities(artifact_store=artifacts, repository=repository)
    worker = Worker(
        client,
        task_queue=REPOSITORY_TASK_QUEUE,
        activities=[
            ingest.ingest_task,
            ingest.finalize_task_spec,
            repository_activities.scan_repository,
        ],
    )
    get_logger(component="worker", queue="repository").info(
        "worker.starting", task_queue=REPOSITORY_TASK_QUEUE
    )
    await worker.run()


async def _run_sandbox_worker() -> None:
    settings = get_settings()
    client = await connect(settings)
    artifacts = await _artifact_store()
    sandbox = DockerSandboxService(
        data_dir=settings.data_dir,
        artifact_store=artifacts,
        tenant_id=settings.local_tenant_id,
    )
    verification = VerificationActivities(
        BaselineVerificationService(artifact_store=artifacts, sandbox_service=sandbox)
    )
    worker = Worker(
        client,
        task_queue=SANDBOX_TASK_QUEUE,
        activities=[verification.verify_baseline],
    )
    get_logger(component="worker", queue="sandbox").info(
        "worker.starting", task_queue=SANDBOX_TASK_QUEUE
    )
    await worker.run()


async def _run(queue: str) -> None:
    if queue == "orchestration":
        await _run_orchestration_worker()
        return
    if queue == "repository":
        await _run_repository_worker()
        return
    if queue == "sandbox":
        await _run_sandbox_worker()
        return
    # model worker 要在角色输出 Artifact 落地后注册；提前起一个空 Worker
    # 只会制造“看起来在运行但什么都不做”的假象。
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

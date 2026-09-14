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

from repopilot.activities.blackboard import BlackboardActivities
from repopilot.activities.developer import DeveloperActivities
from repopilot.activities.finalization import FinalizationActivities
from repopilot.activities.ingest import IngestActivities
from repopilot.activities.investigator import InvestigatorActivities
from repopilot.activities.planning import PlanningActivities
from repopilot.activities.projections import ProjectionActivities
from repopilot.activities.qa import QaActivities
from repopilot.activities.repository import RepositoryActivities
from repopilot.activities.reviewer import ReviewerActivities
from repopilot.activities.verification import VerificationActivities
from repopilot.config import get_settings
from repopilot.infrastructure.artifacts.minio import MinioArtifactStore, build_minio_client
from repopilot.infrastructure.db.engine import get_session_factory
from repopilot.infrastructure.db.model_budget import PostgresModelBudgetStore
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.git.repository_service import GitRepositoryService
from repopilot.infrastructure.model.openai_compatible import OpenAICompatibleProvider
from repopilot.infrastructure.sandbox.docker import DockerSandboxService
from repopilot.infrastructure.temporal.client import connect
from repopilot.logging import configure_logging, get_logger
from repopilot.services.model_gateway import BudgetedModelGateway
from repopilot.services.task_queues import (
    MODEL_TASK_QUEUE,
    ORCHESTRATION_TASK_QUEUE,
    REPOSITORY_TASK_QUEUE,
    SANDBOX_TASK_QUEUE,
)
from repopilot.services.verification_service import (
    BaselineVerificationService,
    CandidateVerificationService,
    SealedTestBaselineService,
)
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
    artifacts = await _artifact_store()
    finalization = FinalizationActivities(
        artifact_store=artifacts,
        usage_reader=PostgresModelBudgetStore(get_session_factory()),
    )

    worker = Worker(
        client,
        task_queue=ORCHESTRATION_TASK_QUEUE,
        workflows=[CodeRepairWorkflow],
        activities=[projection_activities.update_projection, finalization.build_final_report],
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
            repository_activities.build_developer_context,
            repository_activities.integrate_patch,
            repository_activities.export_candidate,
            repository_activities.build_final_diff,
            repository_activities.cleanup_repository,
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
        pypi_proxy_url=settings.pypi_proxy_url,
        pypi_egress_network=settings.pypi_egress_network,
    )
    verification = VerificationActivities(
        BaselineVerificationService(artifact_store=artifacts, sandbox_service=sandbox),
        sealed=SealedTestBaselineService(artifact_store=artifacts, sandbox_service=sandbox),
        candidate=CandidateVerificationService(artifact_store=artifacts, sandbox_service=sandbox),
        sandbox_service=sandbox,
        artifact_store=artifacts,
    )
    worker = Worker(
        client,
        task_queue=SANDBOX_TASK_QUEUE,
        activities=[
            verification.prepare_dependencies,
            verification.verify_baseline,
            verification.verify_sealed_tests_on_base,
            verification.verify_candidate,
            verification.cleanup_sandboxes,
        ],
    )
    get_logger(component="worker", queue="sandbox").info(
        "worker.starting", task_queue=SANDBOX_TASK_QUEUE
    )
    await worker.run()


async def _run_model_worker() -> None:
    settings = get_settings()
    if not settings.model_base_url:
        raise RuntimeError("REPOPILOT_MODEL_BASE_URL 未配置，model worker 无法启动")
    if settings.model_api_key is None or not settings.model_api_key.get_secret_value():
        raise RuntimeError("REPOPILOT_MODEL_API_KEY 未配置，model worker 无法启动")

    client = await connect(settings)
    artifacts = await _artifact_store()
    provider = OpenAICompatibleProvider(
        base_url=settings.model_base_url,
        api_key=settings.model_api_key.get_secret_value(),
        artifact_store=artifacts,
        input_usd_per_million_tokens=settings.model_input_usd_per_million_tokens,
        output_usd_per_million_tokens=settings.model_output_usd_per_million_tokens,
        structured_output_mode=settings.model_structured_output_mode,
    )
    gateway = BudgetedModelGateway(provider, PostgresModelBudgetStore(get_session_factory()))
    planning = PlanningActivities(
        artifact_store=artifacts,
        gateway=gateway,
        model=settings.model_name,
        reservation_usd=settings.model_reservation_usd,
    )
    investigator = InvestigatorActivities(
        artifact_store=artifacts,
        gateway=gateway,
        model=settings.model_name,
        reservation_usd=settings.model_reservation_usd,
    )
    blackboard = BlackboardActivities(artifact_store=artifacts)
    qa = QaActivities(
        artifact_store=artifacts,
        gateway=gateway,
        model=settings.model_name,
        reservation_usd=settings.model_reservation_usd,
    )
    developer = DeveloperActivities(
        artifact_store=artifacts,
        gateway=gateway,
        model=settings.model_name,
        reservation_usd=settings.model_reservation_usd,
        sandbox_service=DockerSandboxService(
            data_dir=settings.data_dir,
            artifact_store=artifacts,
            tenant_id=settings.local_tenant_id,
            pypi_proxy_url=settings.pypi_proxy_url,
            pypi_egress_network=settings.pypi_egress_network,
        ),
    )
    reviewer = ReviewerActivities(
        artifact_store=artifacts,
        gateway=gateway,
        model=settings.model_name,
        reservation_usd=settings.model_reservation_usd,
    )
    worker = Worker(
        client,
        task_queue=MODEL_TASK_QUEUE,
        activities=[
            planning.plan_change,
            planning.plan_change_with_agent,
            investigator.investigate_failure,
            blackboard.publish_blackboard,
            qa.design_sealed_tests,
            developer.develop_patch,
            developer.develop_patch_with_agent,
            reviewer.review_candidate,
            reviewer.review_candidate_with_agent,
        ],
    )
    get_logger(component="worker", queue="model").info(
        "worker.starting", task_queue=MODEL_TASK_QUEUE, model=settings.model_name
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
    if queue == "model":
        await _run_model_worker()
        return
    raise ValueError(f"未知 worker queue: {queue}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="repopilot-worker")
    parser.add_argument("queue", choices=_KNOWN_QUEUES)
    args = parser.parse_args(sys.argv[1:])

    configure_logging()
    asyncio.run(_run(args.queue))


if __name__ == "__main__":
    main()

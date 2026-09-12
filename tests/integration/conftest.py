"""集成测试的公共 fixture：真实 Temporal 测试 Server（时间跳跃）+ 真实
PostgreSQL（testcontainers）+ 真实 Worker。

每个测试函数都拿到一个全新的 WorkflowEnvironment/Worker，虽然比 session
级共享慢一些，但避免了 session 级异步 fixture 和 pytest-asyncio 事件循环
作用域搭配的一堆细节问题——集成测试数量不多，这点开销可以接受。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from testcontainers.community.postgres import PostgresContainer

from repopilot.activities.ingest import FinalizeTaskSpecInput
from repopilot.activities.projections import ProjectionActivities
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import IngestResult
from repopilot.infrastructure.db.models import Base
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.temporal.client import ORCHESTRATION_TASK_QUEUE
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.services.task_queues import REPOSITORY_TASK_QUEUE, SANDBOX_TASK_QUEUE
from repopilot.workflows.code_repair import CodeRepairWorkflow


def _derived_ref(source: ArtifactRef, kind: ArtifactKind) -> ArtifactRef:
    digest = sha256(f"{source.run_id}:{kind.value}".encode()).hexdigest()
    return ArtifactRef(
        artifact_id=uuid4(),
        run_id=source.run_id,
        tenant_id=source.tenant_id,
        kind=kind,
        schema_version="1",
        object_key=f"fake/{source.run_id}/{kind.value}/{digest}",
        sha256=digest,
        size_bytes=1,
        base_revision="a" * 40,
        input_artifact_ids=(source.artifact_id,),
        created_at=datetime.now(UTC),
    )


class FakePipelineActivities:
    @activity.defn(name="ingest_task")
    async def ingest_task(self, request_ref: ArtifactRef) -> IngestResult:
        if request_ref.size_bytes == 0:
            return IngestResult(
                repository_url="https://github.com/owner/repo",
                base_revision="a" * 40,
                proposed_acceptance_criteria=("human must approve",),
                acceptance_criteria_source="heuristic",
                requires_requirements_approval=True,
                task_spec_ref=None,
            )
        return IngestResult(
            repository_url="https://github.com/owner/repo",
            base_revision="a" * 40,
            proposed_acceptance_criteria=("tests pass",),
            acceptance_criteria_source="structured",
            requires_requirements_approval=False,
            task_spec_ref=_derived_ref(request_ref, ArtifactKind.TASK_SPEC),
        )

    @activity.defn(name="finalize_task_spec")
    async def finalize_task_spec(self, payload: FinalizeTaskSpecInput) -> ArtifactRef:
        return _derived_ref(payload.create_request_ref, ArtifactKind.TASK_SPEC)

    @activity.defn(name="scan_repository")
    async def scan_repository(self, task_ref: ArtifactRef) -> ArtifactRef:
        return _derived_ref(task_ref, ArtifactKind.REPOSITORY_SNAPSHOT)

    @activity.defn(name="verify_baseline")
    async def verify_baseline(self, snapshot_ref: ArtifactRef) -> ArtifactRef:
        return _derived_ref(snapshot_ref, ArtifactKind.BASELINE_REPORT)


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest_asyncio.fixture
async def db_engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_container.get_connection_url())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def temporal_client(db_engine: AsyncEngine) -> AsyncIterator[Client]:
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    projection_activities = ProjectionActivities(PostgresRunProjectionStore(session_factory))
    fake_pipeline = FakePipelineActivities()

    async with await WorkflowEnvironment.start_time_skipping(data_converter=data_converter) as env:
        worker = Worker(
            env.client,
            task_queue=ORCHESTRATION_TASK_QUEUE,
            workflows=[CodeRepairWorkflow],
            activities=[projection_activities.update_projection],
        )
        repository_worker = Worker(
            env.client,
            task_queue=REPOSITORY_TASK_QUEUE,
            activities=[
                fake_pipeline.ingest_task,
                fake_pipeline.finalize_task_spec,
                fake_pipeline.scan_repository,
            ],
        )
        sandbox_worker = Worker(
            env.client,
            task_queue=SANDBOX_TASK_QUEUE,
            activities=[fake_pipeline.verify_baseline],
        )
        async with worker, repository_worker, sandbox_worker:
            yield env.client

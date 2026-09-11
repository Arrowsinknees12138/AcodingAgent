"""集成测试的公共 fixture：真实 Temporal 测试 Server（时间跳跃）+ 真实
PostgreSQL（testcontainers）+ 真实 Worker。

每个测试函数都拿到一个全新的 WorkflowEnvironment/Worker，虽然比 session
级共享慢一些，但避免了 session 级异步 fixture 和 pytest-asyncio 事件循环
作用域搭配的一堆细节问题——集成测试数量不多，这点开销可以接受。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from testcontainers.community.postgres import PostgresContainer

from repopilot.activities.projections import ProjectionActivities
from repopilot.infrastructure.db.models import Base
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.temporal.client import ORCHESTRATION_TASK_QUEUE
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.workflows.code_repair import CodeRepairWorkflow


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

    async with await WorkflowEnvironment.start_time_skipping(data_converter=data_converter) as env:
        worker = Worker(
            env.client,
            task_queue=ORCHESTRATION_TASK_QUEUE,
            workflows=[CodeRepairWorkflow],
            activities=[projection_activities.update_projection],
        )
        async with worker:
            yield env.client

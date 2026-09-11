"""Contract 测试的公共 fixture：用 testcontainers 拉起临时 PostgreSQL/MinIO，
测试结束后自动销毁，不依赖开发者本机 `docker compose up` 的状态。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from testcontainers.community.minio import MinioContainer
from testcontainers.community.postgres import PostgresContainer

from repopilot.infrastructure.artifacts.minio import MinioArtifactStore, build_minio_client
from repopilot.infrastructure.db.models import Base

TEST_BUCKET = "repopilot-artifacts-test"


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest.fixture(scope="session")
def minio_container() -> Iterator[MinioContainer]:
    with MinioContainer() as container:
        yield container


@pytest_asyncio.fixture
async def db_engine(postgres_container: PostgresContainer):  # type: ignore[no-untyped-def]
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
async def artifact_store(
    db_engine, minio_container: MinioContainer
) -> AsyncIterator[MinioArtifactStore]:
    config = minio_container.get_config()
    client = build_minio_client(
        f"http://{config['endpoint']}", config["access_key"], config["secret_key"]
    )
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    store = MinioArtifactStore(client=client, bucket=TEST_BUCKET, session_factory=session_factory)
    await store.ensure_bucket()
    yield store

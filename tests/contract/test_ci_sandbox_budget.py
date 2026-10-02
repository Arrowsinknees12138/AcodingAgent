"""Real PostgreSQL concurrency and conservative crash accounting."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from repopilot.infrastructure.db.sandbox_budget import PostgresSandboxBudgetStore
from repopilot.services.sandbox_budget import SandboxBudgetExceeded

pytestmark = pytest.mark.contract


async def test_parallel_workers_cannot_over_reserve(db_engine):
    store = PostgresSandboxBudgetStore(async_sessionmaker(db_engine, expire_on_commit=False))
    tenant, run = uuid4(), uuid4()
    await store.initialize(tenant, run, 10)
    values = await asyncio.gather(
        store.reserve(run, 5), store.reserve(run, 5), return_exceptions=True
    )
    assert values.count(5) == 1
    assert sum(isinstance(v, SandboxBudgetExceeded) for v in values) == 1
    assert await store.usage(run) == (0, 10000)
    # Lost worker retains reservation; no new command is authorized.
    with pytest.raises(SandboxBudgetExceeded):
        await store.reserve(run, 1)
    await store.settle(run, 5, 1500)
    assert await store.usage(run) == (1500, 0)
    assert await store.reserve(run, 600) == 3


async def test_retry_does_not_reset_budget_and_old_runs_are_compatible(db_engine):
    store = PostgresSandboxBudgetStore(async_sessionmaker(db_engine, expire_on_commit=False))
    tenant, run = uuid4(), uuid4()
    assert await store.reserve(run, 5) is None
    await store.initialize(tenant, run, 10)
    await store.initialize(tenant, run, 100)
    assert await store.reserve(run, 600) == 5

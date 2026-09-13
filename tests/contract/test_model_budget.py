"""真实 PostgreSQL 上验证模型预算的事务状态转换。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from repopilot.domain.artifacts import ArtifactRef, build_object_key
from repopilot.domain.enums import ArtifactKind
from repopilot.infrastructure.db.model_budget import PostgresModelBudgetStore
from repopilot.infrastructure.db.models import ModelCall, RunBudget
from repopilot.services.model_gateway import (
    BudgetExceededError,
    ModelCallContext,
    ModelUsage,
)

pytestmark = pytest.mark.contract


async def test_reserve_settle_and_enforce_call_limit(db_engine) -> None:  # type: ignore[no-untyped-def]
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    store = PostgresModelBudgetStore(sessions)
    tenant_id, run_id, work_item_id = uuid4(), uuid4(), uuid4()
    await store.create_budget(
        run_id=run_id,
        tenant_id=tenant_id,
        max_cost_usd=Decimal("1"),
        max_model_calls=1,
    )
    # Idempotent API retries must not reset a budget that already exists.
    await store.create_budget(
        run_id=run_id,
        tenant_id=tenant_id,
        max_cost_usd=Decimal("9"),
        max_model_calls=9,
    )
    context = ModelCallContext(
        model_call_id=uuid4(),
        tenant_id=tenant_id,
        run_id=run_id,
        work_item_id=work_item_id,
        reservation_usd=Decimal("0.10"),
    )
    reservation = await store.reserve(
        context=context,
        logical_call_key="a" * 64,
        provider="fake",
        model="fake-model",
    )
    assert reservation.created is True
    replay = await store.reserve(
        context=context.model_copy(update={"model_call_id": uuid4()}),
        logical_call_key="a" * 64,
        provider="fake",
        model="fake-model",
    )
    assert replay.created is False
    assert replay.model_call_id == context.model_call_id
    digest = "b" * 64
    response_ref = ArtifactRef(
        artifact_id=uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        kind=ArtifactKind.MODEL_RESPONSE,
        schema_version="1",
        object_key=build_object_key(tenant_id, run_id, ArtifactKind.MODEL_RESPONSE, digest),
        sha256=digest,
        size_bytes=1,
        base_revision="c" * 40,
        input_artifact_ids=(),
        created_at=datetime.now(UTC),
    )
    await store.settle(
        context=context,
        usage=ModelUsage(
            input_tokens=10,
            output_tokens=5,
            cost_usd=Decimal("0.01"),
        ),
        response_ref=response_ref,
    )
    usage = await store.get_run_usage(run_id=run_id, tenant_id=tenant_id)
    assert usage.model_calls == 1
    assert usage.input_tokens == 10
    assert usage.output_tokens == 5
    assert usage.cost_usd == Decimal("0.01")
    assert usage.unknown_calls == 0

    async with sessions() as session:
        budget = await session.get(RunBudget, run_id)
        call = await session.get(ModelCall, context.model_call_id)
        assert budget is not None
        assert budget.max_cost_usd == Decimal("1")
        assert budget.max_model_calls == 1
        assert budget.reserved_calls == 0
        assert budget.settled_calls == 1
        assert budget.spent_usd == Decimal("0.01")
        assert call is not None
        assert call.status == "SETTLED"
        assert call.response_artifact_id == response_ref.artifact_id

    with pytest.raises(BudgetExceededError):
        await store.reserve(
            context=ModelCallContext(
                model_call_id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                work_item_id=work_item_id,
                reservation_usd=Decimal("0.10"),
            ),
            logical_call_key="d" * 64,
            provider="fake",
            model="fake-model",
        )


async def test_concurrent_reservations_cannot_oversubscribe_call_budget(
    db_engine,
) -> None:  # type: ignore[no-untyped-def]
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    store = PostgresModelBudgetStore(sessions)
    tenant_id, run_id = uuid4(), uuid4()
    await store.create_budget(
        run_id=run_id,
        tenant_id=tenant_id,
        max_cost_usd=Decimal("1"),
        max_model_calls=1,
    )

    async def reserve(suffix: str):
        return await store.reserve(
            context=ModelCallContext(
                model_call_id=uuid4(),
                tenant_id=tenant_id,
                run_id=run_id,
                work_item_id=uuid4(),
                reservation_usd=Decimal("0.10"),
            ),
            logical_call_key=suffix * 64,
            provider="fake",
            model="fake-model",
        )

    results = await asyncio.gather(reserve("c"), reserve("d"), return_exceptions=True)
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, BudgetExceededError) for result in results) == 1

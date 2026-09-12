"""PostgreSQL 模型预算预留/结算实现（实施设计第 11.3 节）。"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repopilot.domain.artifacts import ArtifactRef
from repopilot.infrastructure.db.models import ModelCall, RunBudget
from repopilot.services.model_gateway import (
    BudgetExceededError,
    ModelBudgetStore,
    ModelCallContext,
    ModelCallReservation,
    ModelUsage,
)


class BudgetStateError(RuntimeError):
    """预算或调用记录不存在，或状态转换非法。"""


class PostgresModelBudgetStore(ModelBudgetStore):
    def __init__(self, session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
        self._session_factory = session_factory

    async def create_budget(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        max_cost_usd: Decimal,
        max_model_calls: int,
    ) -> None:
        if max_cost_usd <= 0 or max_model_calls <= 0:
            raise ValueError("模型预算必须为正数")
        async with self._session_factory() as session, session.begin():
            if await session.get(RunBudget, run_id) is not None:
                return
            session.add(
                RunBudget(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    max_cost_usd=max_cost_usd,
                    max_model_calls=max_model_calls,
                    spent_usd=Decimal("0"),
                    reserved_usd=Decimal("0"),
                    settled_calls=0,
                    reserved_calls=0,
                    version=1,
                )
            )

    async def reserve(
        self,
        *,
        context: ModelCallContext,
        logical_call_key: str,
        provider: str,
        model: str,
    ) -> ModelCallReservation:
        now = datetime.now(UTC)
        async with self._session_factory() as session, session.begin():
            # 先锁 RunBudget，把同一 Run 下的“查重 + 容量判断 + 插入”
            # 串行化；否则两个并发事务都可能先查到不存在，最后撞唯一约束。
            budget = await session.scalar(
                select(RunBudget)
                .where(
                    RunBudget.run_id == context.run_id,
                    RunBudget.tenant_id == context.tenant_id,
                )
                .with_for_update()
            )
            if budget is None:
                raise BudgetStateError(f"run {context.run_id} 尚未初始化模型预算")
            existing = await session.scalar(
                select(ModelCall).where(ModelCall.logical_call_key == logical_call_key)
            )
            if existing is not None:
                return ModelCallReservation(
                    model_call_id=existing.model_call_id,
                    logical_call_key=existing.logical_call_key,
                    status=existing.status,
                    created=False,
                )
            projected_cost = budget.spent_usd + budget.reserved_usd + context.reservation_usd
            if projected_cost > budget.max_cost_usd:
                raise BudgetExceededError("模型费用预算不足")
            if budget.settled_calls + budget.reserved_calls >= budget.max_model_calls:
                raise BudgetExceededError("模型调用次数预算不足")

            budget.reserved_usd += context.reservation_usd
            budget.reserved_calls += 1
            budget.version += 1
            session.add(
                ModelCall(
                    model_call_id=context.model_call_id,
                    logical_call_key=logical_call_key,
                    tenant_id=context.tenant_id,
                    run_id=context.run_id,
                    work_item_id=context.work_item_id,
                    provider=provider,
                    model=model,
                    status="RESERVED",
                    reserved_usd=context.reservation_usd,
                    actual_usd=None,
                    input_tokens=None,
                    output_tokens=None,
                    response_artifact_id=None,
                    created_at=now,
                    updated_at=now,
                )
            )
        return ModelCallReservation(
            model_call_id=context.model_call_id,
            logical_call_key=logical_call_key,
            status="RESERVED",
            created=True,
        )

    async def settle(
        self, *, context: ModelCallContext, usage: ModelUsage, response_ref: ArtifactRef
    ) -> None:
        async with self._session_factory() as session, session.begin():
            call, budget = await self._lock_call_and_budget(session, context.model_call_id)
            if call.status != "RESERVED":
                raise BudgetStateError(f"只能结算 RESERVED 调用，当前为 {call.status}")
            if usage.cost_usd > call.reserved_usd:
                raise BudgetStateError("实际费用超过预留上限，保留 reservation 等待人工处理")
            call.status = "SETTLED"
            call.actual_usd = usage.cost_usd
            call.input_tokens = usage.input_tokens
            call.output_tokens = usage.output_tokens
            call.response_artifact_id = response_ref.artifact_id
            call.updated_at = datetime.now(UTC)
            budget.reserved_usd -= call.reserved_usd
            budget.reserved_calls -= 1
            budget.spent_usd += usage.cost_usd
            budget.settled_calls += 1
            budget.version += 1
            self._check_non_negative(budget)

    async def release(self, model_call_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            call, budget = await self._lock_call_and_budget(session, model_call_id)
            if call.status != "RESERVED":
                return
            call.status = "RELEASED"
            call.updated_at = datetime.now(UTC)
            budget.reserved_usd -= call.reserved_usd
            budget.reserved_calls -= 1
            budget.version += 1
            self._check_non_negative(budget)

    async def mark_unknown(self, model_call_id: UUID) -> None:
        async with self._session_factory() as session, session.begin():
            call = await session.scalar(
                select(ModelCall).where(ModelCall.model_call_id == model_call_id).with_for_update()
            )
            if call is None:
                raise BudgetStateError(f"model call {model_call_id} 不存在")
            if call.status == "RESERVED":
                call.status = "UNKNOWN"
                call.updated_at = datetime.now(UTC)

    async def _lock_call_and_budget(
        self, session: AsyncSession, model_call_id: UUID
    ) -> tuple[ModelCall, RunBudget]:
        call = await session.scalar(
            select(ModelCall).where(ModelCall.model_call_id == model_call_id).with_for_update()
        )
        if call is None:
            raise BudgetStateError(f"model call {model_call_id} 不存在")
        budget = await session.scalar(
            select(RunBudget).where(RunBudget.run_id == call.run_id).with_for_update()
        )
        if budget is None:
            raise BudgetStateError(f"run {call.run_id} 的预算不存在")
        return call, budget

    @staticmethod
    def _check_non_negative(budget: RunBudget) -> None:
        if budget.reserved_usd < 0 or budget.reserved_calls < 0:
            raise BudgetStateError("预算计数不能为负数")

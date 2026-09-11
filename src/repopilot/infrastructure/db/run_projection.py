"""`RunProjectionStore` 的 PostgreSQL 实现（实施设计第 17.1、17.2 节）。

用 `INSERT ... ON CONFLICT (run_id) DO UPDATE` 做 upsert：`run_projections`
本质是"这个 Run 当前状态是什么"的单行快照，重复应用相同内容的事件天然
幂等，不需要额外的事件去重表。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import async_sessionmaker

from repopilot.domain.enums import RunStatus
from repopilot.infrastructure.db.models import RunProjection
from repopilot.services.run_projection import ProjectionEvent, is_terminal


class PostgresRunProjectionStore:
    def __init__(self, session_factory: async_sessionmaker) -> None:  # type: ignore[type-arg]
        self._session_factory = session_factory

    async def apply(self, event: ProjectionEvent) -> None:
        terminal_at = event.occurred_at if is_terminal(event.status) else None
        stmt = insert(RunProjection).values(
            run_id=event.run_id,
            tenant_id=event.tenant_id,
            workflow_id=event.workflow_id,
            status=event.status.value,
            base_revision=event.base_revision,
            plan_version=event.plan_version,
            model_calls=event.model_calls,
            created_at=event.occurred_at,
            updated_at=event.occurred_at,
            terminal_at=terminal_at,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[RunProjection.run_id],
            set_={
                "status": stmt.excluded.status,
                "base_revision": stmt.excluded.base_revision,
                "plan_version": stmt.excluded.plan_version,
                "model_calls": stmt.excluded.model_calls,
                "updated_at": stmt.excluded.updated_at,
                "terminal_at": stmt.excluded.terminal_at,
            },
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def get(self, run_id: UUID) -> ProjectionEvent | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(RunProjection).where(RunProjection.run_id == run_id))
        if row is None:
            return None
        return ProjectionEvent(
            run_id=row.run_id,
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            status=RunStatus(row.status),
            base_revision=row.base_revision,
            plan_version=row.plan_version,
            model_calls=row.model_calls,
            occurred_at=row.updated_at,
        )

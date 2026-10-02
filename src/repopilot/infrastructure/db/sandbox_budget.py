"""Reserve command timeout under a row lock before starting untrusted code."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from repopilot.infrastructure.db.models import SandboxBudget
from repopilot.services.sandbox_budget import SandboxBudgetExceeded


class PostgresSandboxBudgetStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def initialize(self, tenant_id: UUID, run_id: UUID, max_seconds: int) -> None:
        async with self.sessions() as session, session.begin():
            await session.execute(
                insert(SandboxBudget)
                .values(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    max_ms=max_seconds * 1000,
                    consumed_ms=0,
                    reserved_ms=0,
                )
                .on_conflict_do_nothing(index_elements=[SandboxBudget.run_id])
            )

    async def reserve(self, run_id: UUID, timeout_seconds: int) -> int | None:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(SandboxBudget).where(SandboxBudget.run_id == run_id).with_for_update()
            )
            if row is None:  # Historical/non-CI runs retain their previous behavior.
                return None
            granted = min(
                timeout_seconds, (row.max_ms - row.consumed_ms - row.reserved_ms) // 1000 - 5
            )
            if granted < 1:
                raise SandboxBudgetExceeded("Sandbox command-time budget exhausted")
            row.reserved_ms += (granted + 5) * 1000
            return granted

    async def settle(self, run_id: UUID, granted_seconds: int, duration_ms: int) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.scalar(
                select(SandboxBudget).where(SandboxBudget.run_id == run_id).with_for_update()
            )
            if row is None:
                raise RuntimeError("Missing sandbox budget")
            row.reserved_ms -= (granted_seconds + 5) * 1000
            row.consumed_ms += max(0, duration_ms)

    async def usage(self, run_id: UUID) -> tuple[int, int] | None:
        async with self.sessions() as session:
            row = await session.get(SandboxBudget, run_id)
            return (row.consumed_ms, row.reserved_ms) if row else None

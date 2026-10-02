"""Cross-worker command-time budget contract."""

from typing import Protocol
from uuid import UUID


class SandboxBudgetExceeded(RuntimeError):
    pass


class SandboxBudgetStore(Protocol):
    async def reserve(self, run_id: UUID, timeout_seconds: int) -> int | None: ...

    async def settle(self, run_id: UUID, granted_seconds: int, duration_ms: int) -> None: ...

    async def usage(self, run_id: UUID) -> tuple[int, int] | None: ...

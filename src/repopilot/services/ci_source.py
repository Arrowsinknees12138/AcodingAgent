"""GitHub source port; API can inspect metadata, workers collect bounded logs."""

from __future__ import annotations

from typing import Protocol

from pydantic import Field

from repopilot.domain import StrictModel


class CiSourceError(RuntimeError):
    """Safe user-facing upstream error (never includes response bodies or tokens)."""


class CiJob(StrictModel):
    job_id: int
    name: str
    conclusion: str | None
    labels: tuple[str, ...]


class CiRun(StrictModel):
    repository_url: str
    repository_id: int
    workflow_id: int
    run_id: int
    run_attempt: int
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    event: str
    status: str
    conclusion: str | None
    jobs: tuple[CiJob, ...]

    def canonical_url(self) -> str:
        return f"{self.repository_url}/actions/runs/{self.run_id}/attempts/{self.run_attempt}"


class CiSourceReader(Protocol):
    async def inspect(self, run_url: str) -> CiRun: ...

    async def job_log(self, run: CiRun, job_id: int) -> str: ...

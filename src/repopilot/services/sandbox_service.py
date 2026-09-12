"""Sandbox Service 的协议与数据契约（实施设计第 10.4、14.1 节）。

`RunCommandRequest` 本来是 Tool Registry（Milestone 6 的 `tools/run_command.py`）
的输入契约，但 Sandbox Service 在 Milestone 5 就需要用它执行基线/候选验证
命令（compileall、pytest、ruff、mypy），所以先在这里定义，Milestone 6
直接复用，不重复定义一遍。
"""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from pydantic import Field

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef


class RunCommandRequest(StrictModel):
    executable: Literal["python", "pytest", "ruff", "mypy"]
    args: tuple[str, ...]
    cwd: str = Field(min_length=1)
    timeout_seconds: int = Field(gt=0, le=600)


class SandboxSpec(StrictModel):
    run_id: UUID
    work_item_id: UUID | None
    image: str
    source_archive_ref: ArtifactRef
    test_bundle_ref: ArtifactRef | None
    network_enabled: bool = False
    cpu_limit: float = 1.0
    memory_mb: int = 1024
    pids_limit: int = 128
    disk_mb: int = 2048
    wall_time_seconds: int = 600


class CommandResult(StrictModel):
    exit_code: int | None
    timed_out: bool
    oom_killed: bool
    duration_ms: int
    stdout_ref: ArtifactRef
    stderr_ref: ArtifactRef


class SandboxService(Protocol):
    async def create(self, spec: SandboxSpec) -> UUID: ...

    async def execute(self, sandbox_id: UUID, request: RunCommandRequest) -> CommandResult: ...

    async def export_changes(self, sandbox_id: UUID) -> ArtifactRef: ...

    async def destroy(self, sandbox_id: UUID) -> None: ...

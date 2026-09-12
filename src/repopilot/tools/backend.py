"""Tool 与 sandbox/repository RPC adapter 之间的窄接口。"""

from __future__ import annotations

from typing import Protocol

from repopilot.services.sandbox_service import CommandResult, RunCommandRequest
from repopilot.tools.registry import ToolContext
from repopilot.tools.workspace_models import (
    ReadFileRequest,
    ReadFileResult,
    SearchCodeRequest,
    SearchCodeResult,
    WriteFileRequest,
    WriteFileResult,
)


class WorkspaceToolBackend(Protocol):
    async def read_file(self, request: ReadFileRequest, context: ToolContext) -> ReadFileResult: ...

    async def search_code(
        self, request: SearchCodeRequest, context: ToolContext
    ) -> SearchCodeResult: ...

    async def write_file(
        self, request: WriteFileRequest, context: ToolContext
    ) -> WriteFileResult: ...

    async def run_command(
        self, request: RunCommandRequest, context: ToolContext
    ) -> CommandResult: ...

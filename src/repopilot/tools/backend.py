"""Tool 与 sandbox/repository RPC adapter 之间的窄接口。"""

from __future__ import annotations

from typing import Protocol

from repopilot.services.sandbox_service import RunCommandRequest
from repopilot.tools.registry import ToolContext
from repopilot.tools.workspace_models import (
    DeleteFileRequest,
    DeleteFileResult,
    ReadFileRequest,
    ReadFileResult,
    SearchCodeRequest,
    SearchCodeResult,
    WorkspaceCommandResult,
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

    async def delete_file(
        self, request: DeleteFileRequest, context: ToolContext
    ) -> DeleteFileResult: ...

    async def run_command(
        self, request: RunCommandRequest, context: ToolContext
    ) -> WorkspaceCommandResult: ...

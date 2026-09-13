"""Delete only an explicitly writable file in the agent's isolated workspace."""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.tools.backend import WorkspaceToolBackend
from repopilot.tools.paths import require_allowed_path
from repopilot.tools.registry import ToolContext
from repopilot.tools.workspace_models import DeleteFileRequest


class DeleteFileTool:
    name = "delete_file"
    input_model: type[BaseModel] = DeleteFileRequest

    def __init__(self, backend: WorkspaceToolBackend) -> None:
        self._backend = backend

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = DeleteFileRequest.model_validate(request)
        normalized = require_allowed_path(typed.path, context.write_paths)
        return await self._backend.delete_file(
            typed.model_copy(update={"path": normalized}), context
        )

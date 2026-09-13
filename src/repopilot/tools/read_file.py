"""受 read allowlist 约束的 read_file 工具。"""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.tools.backend import WorkspaceToolBackend
from repopilot.tools.paths import normalize_relative_path, require_allowed_path
from repopilot.tools.registry import ToolContext
from repopilot.tools.workspace_models import ReadFileRequest


class ReadFileTool:
    name = "read_file"
    input_model: type[BaseModel] = ReadFileRequest

    def __init__(self, backend: WorkspaceToolBackend) -> None:
        self._backend = backend

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = ReadFileRequest.model_validate(request)
        normalized = (
            normalize_relative_path(typed.path)
            if context.read_all_repository_files
            else require_allowed_path(
                typed.path, tuple([*context.read_paths, *context.write_paths])
            )
        )
        return await self._backend.read_file(typed.model_copy(update={"path": normalized}), context)

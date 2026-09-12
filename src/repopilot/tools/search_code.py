"""限制检索范围的 search_code 工具。"""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.tools.backend import WorkspaceToolBackend
from repopilot.tools.paths import require_allowed_path
from repopilot.tools.registry import ToolContext
from repopilot.tools.workspace_models import SearchCodeRequest


class SearchCodeTool:
    name = "search_code"
    input_model: type[BaseModel] = SearchCodeRequest

    def __init__(self, backend: WorkspaceToolBackend) -> None:
        self._backend = backend

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = SearchCodeRequest.model_validate(request)
        requested = typed.paths or context.read_paths
        paths = tuple(require_allowed_path(path, context.read_paths) for path in requested)
        return await self._backend.search_code(typed.model_copy(update={"paths": paths}), context)

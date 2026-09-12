"""受精确 write allowlist 和大小上限约束的 write_file 工具。"""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.tools.backend import WorkspaceToolBackend
from repopilot.tools.paths import require_allowed_path
from repopilot.tools.registry import ToolContext
from repopilot.tools.workspace_models import WriteFileRequest

_MAX_FILE_BYTES = 1024 * 1024


class WriteFileTool:
    name = "write_file"
    input_model: type[BaseModel] = WriteFileRequest

    def __init__(self, backend: WorkspaceToolBackend) -> None:
        self._backend = backend

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = WriteFileRequest.model_validate(request)
        normalized = require_allowed_path(typed.path, context.write_paths)
        if "\x00" in typed.content:
            raise ValueError("拒绝包含 NUL 的二进制文件内容")
        if len(typed.content.encode("utf-8")) > _MAX_FILE_BYTES:
            raise ValueError("单文件不能超过 1 MiB")
        return await self._backend.write_file(
            typed.model_copy(update={"path": normalized}), context
        )

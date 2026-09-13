"""无 Shell 字符串、固定 executable 的 run_command 工具。"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from pydantic import BaseModel

from repopilot.services.sandbox_service import RunCommandRequest
from repopilot.tools.backend import WorkspaceToolBackend
from repopilot.tools.paths import normalize_relative_path
from repopilot.tools.registry import ToolContext


class RunCommandTool:
    name = "run_command"
    input_model: type[BaseModel] = RunCommandRequest

    def __init__(self, backend: WorkspaceToolBackend) -> None:
        self._backend = backend

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        typed = RunCommandRequest.model_validate(request)
        _validate_arguments(typed)
        normalized_cwd = (
            "." if typed.cwd in {".", "/workspace"} else normalize_relative_path(typed.cwd)
        )
        if context.sandbox_id is None and not context.command_sandbox_on_demand:
            raise ValueError("run_command 需要已创建的 sandbox")
        return await self._backend.run_command(
            typed.model_copy(update={"cwd": normalized_cwd}), context
        )


def _validate_arguments(request: RunCommandRequest) -> None:
    forbidden = {"-c", "-m pip", "--config", "--config-file", "-p"}
    joined = " ".join(request.args)
    if any(token in forbidden for token in request.args) or "-m pip" in joined:
        raise ValueError("命令参数包含被禁止的解释器/插件/配置入口")
    for argument in request.args:
        values = (argument, argument.partition("=")[2])
        for value in values:
            if not value:
                continue
            normalized = value.replace("\\", "/")
            if (
                "\x00" in value
                or PurePosixPath(value).is_absolute()
                or PureWindowsPath(value).is_absolute()
                or ".." in normalized.split("/")
            ):
                raise ValueError("命令参数只能引用 workspace 相对路径")
    if request.executable == "python":
        is_compileall = request.args[:2] == ("-m", "compileall")
        is_script = bool(request.args) and request.args[0].endswith(".py")
        if not (is_compileall or is_script):
            raise ValueError("python 只允许 -m compileall 或显式 workspace 脚本")

"""所有角色用于提交结构化最终结果的 finish 工具。"""

from __future__ import annotations

from pydantic import BaseModel

from repopilot.domain.agents import FinishRequest
from repopilot.tools.registry import ToolContext


class FinishTool:
    name = "finish"
    input_model: type[BaseModel] = FinishRequest

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel:
        del context
        return request

"""Tool Registry：角色 allowlist、输入 Schema 校验和统一执行入口。"""

from __future__ import annotations

import json
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ValidationError

from repopilot.domain import StrictModel
from repopilot.domain.enums import AgentRole


class ToolContext(StrictModel):
    tenant_id: UUID
    run_id: UUID
    work_item_id: UUID
    sandbox_id: UUID | None
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]
    read_all_repository_files: bool = False
    command_sandbox_on_demand: bool = False


class Tool(Protocol):
    name: str
    input_model: type[BaseModel]

    async def execute(self, request: BaseModel, context: ToolContext) -> BaseModel: ...


class ToolRegistryError(RuntimeError):
    pass


class ToolNotAllowedError(ToolRegistryError):
    pass


class ToolInputError(ToolRegistryError):
    pass


_ROLE_TOOLS: dict[AgentRole, frozenset[str]] = {
    AgentRole.PLANNER: frozenset({"read_file", "search_code", "submit_plan", "finish"}),
    AgentRole.INVESTIGATOR: frozenset(
        {"read_file", "search_code", "submit_investigation", "finish"}
    ),
    AgentRole.QA: frozenset({"read_file", "search_code", "finish"}),
    AgentRole.DEVELOPER: frozenset(
        {"read_file", "search_code", "write_file", "delete_file", "run_command", "finish"}
    ),
    AgentRole.REVIEWER: frozenset({"read_file", "finish"}),
}


class ToolRegistry:
    def __init__(self, tools: tuple[Tool, ...]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("Tool name 必须唯一")

    def validate_requested_tools(self, role: AgentRole, requested: tuple[str, ...]) -> None:
        unknown = set(requested) - set(self._tools)
        denied = set(requested) - _ROLE_TOOLS[role]
        if unknown:
            raise ToolNotAllowedError(f"未注册工具: {', '.join(sorted(unknown))}")
        if denied:
            raise ToolNotAllowedError(f"{role.value} 角色不能使用: {', '.join(sorted(denied))}")

    async def execute(
        self,
        *,
        role: AgentRole,
        allowed_tools: tuple[str, ...],
        name: str,
        arguments: dict[str, object],
        context: ToolContext,
    ) -> BaseModel:
        self.validate_requested_tools(role, allowed_tools)
        if name not in allowed_tools or name not in _ROLE_TOOLS[role]:
            raise ToolNotAllowedError(f"{role.value} 本次执行未获准使用 {name}")
        tool = self._tools[name]
        try:
            request = tool.input_model.model_validate_json(json.dumps(arguments))
        except (TypeError, ValueError, ValidationError) as exc:
            raise ToolInputError(f"{name} 参数不符合 Schema: {exc}") from exc
        try:
            return await tool.execute(request, context)
        except PermissionError as exc:
            raise ToolNotAllowedError(f"{name} 被策略拒绝: {exc}") from exc
        except ValueError as exc:
            raise ToolInputError(f"{name} 参数被拒绝: {exc}") from exc

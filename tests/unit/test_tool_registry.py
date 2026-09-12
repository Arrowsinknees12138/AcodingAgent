"""Tool Registry 的角色矩阵与 Schema 校验。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import BaseModel

from repopilot.domain.enums import AgentRole
from repopilot.tools.finish import FinishTool
from repopilot.tools.registry import (
    ToolContext,
    ToolInputError,
    ToolNotAllowedError,
    ToolRegistry,
)


def _context() -> ToolContext:
    return ToolContext(
        tenant_id=uuid4(),
        run_id=uuid4(),
        work_item_id=uuid4(),
        sandbox_id=None,
        read_paths=(),
        write_paths=(),
    )


async def test_finish_validates_and_returns_structured_request() -> None:
    registry = ToolRegistry((FinishTool(),))
    result = await registry.execute(
        role=AgentRole.REVIEWER,
        allowed_tools=("finish",),
        name="finish",
        arguments={"status": "succeeded", "output_refs": [], "error": None},
        context=_context(),
    )
    assert isinstance(result, BaseModel)
    assert result.model_dump()["status"] == "succeeded"


async def test_unknown_requested_tool_fails_closed() -> None:
    registry = ToolRegistry((FinishTool(),))
    with pytest.raises(ToolNotAllowedError):
        registry.validate_requested_tools(AgentRole.PLANNER, ("finish", "unknown"))


async def test_invalid_finish_arguments_are_rejected() -> None:
    registry = ToolRegistry((FinishTool(),))
    with pytest.raises(ToolInputError):
        await registry.execute(
            role=AgentRole.PLANNER,
            allowed_tools=("finish",),
            name="finish",
            arguments={"status": "maybe"},
            context=_context(),
        )

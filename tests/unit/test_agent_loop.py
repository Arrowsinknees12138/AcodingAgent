"""Fake Model + Artifact + Budget + Tool Registry 的 Agent 闭环测试。"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from repopilot.agents.loop import AgentLoop
from repopilot.domain.agents import AgentExecutionRequest, AgentTurn
from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.enums import AgentRole, ArtifactKind
from repopilot.domain.errors import ErrorCode
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.services.model_gateway import BudgetedModelGateway
from repopilot.tools.finish import FinishTool
from repopilot.tools.registry import ToolContext, ToolRegistry
from tests.fakes import MemoryArtifactStore, RecordingBudgetStore


async def test_fake_model_completes_developer_agent_and_settles_budget() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id, work_item_id = uuid4(), uuid4(), uuid4()
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        b"def answer(): return 42",
        ArtifactMetadata(
            tenant_id=tenant_id,
            run_id=run_id,
            base_revision="a" * 40,
            schema_version="1",
        ),
    )
    provider = FakeModelProvider(
        [AgentTurn(tool="finish", arguments={"status": "succeeded"})],
        store,
    )
    budget = RecordingBudgetStore()
    loop = AgentLoop(
        gateway=BudgetedModelGateway(provider, budget),
        artifact_store=store,
        tools=ToolRegistry((FinishTool(),)),
        model="fake-model",
        reservation_usd=Decimal("0.01"),
    )
    result = await loop.execute(
        AgentExecutionRequest(
            role=AgentRole.DEVELOPER,
            work_item_id=work_item_id,
            input_refs=(source_ref,),
            allowed_tools=("finish",),
            max_steps=2,
            remaining_model_calls=2,
        ),
        ToolContext(
            tenant_id=tenant_id,
            run_id=run_id,
            work_item_id=work_item_id,
            sandbox_id=None,
            read_paths=("src/example.py",),
            write_paths=("src/example.py",),
        ),
    )

    assert result.status == "succeeded"
    assert len(result.model_call_ids) == 1
    assert budget.reserved == list(result.model_call_ids)
    assert budget.settled == list(result.model_call_ids)
    assert provider.requests[0].system_prompt.startswith("---")
    assert any(ref.kind == ArtifactKind.TRAJECTORY for ref in store.refs)


async def test_invalid_model_outputs_are_billed_and_stop_after_two() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id, work_item_id = uuid4(), uuid4(), uuid4()
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        b"source",
        ArtifactMetadata(
            tenant_id=tenant_id,
            run_id=run_id,
            base_revision="a" * 40,
            schema_version="1",
        ),
    )
    provider = FakeModelProvider([{"arguments": {}}, {"tool": "", "arguments": {}}], store)
    budget = RecordingBudgetStore()
    loop = AgentLoop(
        gateway=BudgetedModelGateway(provider, budget),
        artifact_store=store,
        tools=ToolRegistry((FinishTool(),)),
        model="fake-model",
        reservation_usd=Decimal("0.01"),
    )
    result = await loop.execute(
        AgentExecutionRequest(
            role=AgentRole.DEVELOPER,
            work_item_id=work_item_id,
            input_refs=(source_ref,),
            allowed_tools=("finish",),
            max_steps=2,
            remaining_model_calls=2,
        ),
        ToolContext(
            tenant_id=tenant_id,
            run_id=run_id,
            work_item_id=work_item_id,
            sandbox_id=None,
            read_paths=("src.py",),
            write_paths=("src.py",),
        ),
    )

    assert result.status == "failed"
    assert result.error is not None
    assert result.error.code == ErrorCode.MODEL_OUTPUT_INVALID
    assert budget.settled == list(result.model_call_ids)
    assert budget.released == []

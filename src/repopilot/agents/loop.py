"""统一 Agent Loop（实施设计第 9.2 节）。"""

from __future__ import annotations

import json
from decimal import Decimal
from importlib.resources import files
from uuid import UUID, uuid4

from pydantic import ValidationError

from repopilot.domain.agents import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentTurn,
    FinishRequest,
)
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import AgentRole, ArtifactKind
from repopilot.domain.errors import ErrorCode, ErrorInfo
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.model_gateway import (
    BudgetedModelGateway,
    BudgetExceededError,
    ModelCallContext,
    ModelCompletionUnknownError,
    ModelOutputInvalidError,
    ModelProviderError,
    ModelRequest,
    build_logical_call_key,
)
from repopilot.tools.registry import (
    ToolContext,
    ToolInputError,
    ToolNotAllowedError,
    ToolRegistry,
)

_ROLE_MODEL_CALL_LIMIT = {
    AgentRole.PLANNER: 3,
    AgentRole.QA: 5,
    AgentRole.DEVELOPER: 10,
    AgentRole.REVIEWER: 3,
}


class AgentLoop:
    def __init__(
        self,
        *,
        gateway: BudgetedModelGateway,
        artifact_store: ArtifactStore,
        tools: ToolRegistry,
        model: str,
        reservation_usd: Decimal,
    ) -> None:
        self._gateway = gateway
        self._artifact_store = artifact_store
        self._tools = tools
        self._model = model
        self._reservation_usd = reservation_usd

    async def execute(
        self, request: AgentExecutionRequest, tool_context: ToolContext
    ) -> AgentExecutionResult:
        self._validate_context(request, tool_context)
        self._tools.validate_requested_tools(request.role, request.allowed_tools)
        messages = await self._load_inputs(request)
        prompt = _load_prompt(request.role)
        call_limit = min(
            request.remaining_model_calls,
            _ROLE_MODEL_CALL_LIMIT[request.role],
            request.max_steps,
        )
        call_ids: list[UUID] = []
        consecutive_format_errors = 0

        for step in range(call_limit):
            messages_ref = await self._save_trajectory(request, messages)
            call_id = uuid4()
            logical_key = build_logical_call_key(
                tenant_id=messages_ref.tenant_id,
                work_item_id=request.work_item_id,
                attempt=request.attempt,
                provider=self._gateway.provider.name,
                model=self._model,
                model_parameters={
                    "temperature": 0.0,
                    "top_p": 1.0,
                    "max_output_tokens": 4096,
                    "step": step,
                },
                prompt_version=request.prompt_version,
                tool_schema_version=request.tool_schema_version,
                ordered_input_artifact_hashes=tuple(
                    [*(ref.sha256 for ref in request.input_refs), messages_ref.sha256]
                ),
                policy_version=request.policy_version,
            )
            model_request = ModelRequest(
                logical_call_key=logical_key,
                model=self._model,
                system_prompt=prompt,
                messages_ref=messages_ref,
                tool_schema_ref=None,
                temperature=0.0,
                top_p=1.0,
                max_output_tokens=4096,
            )
            call_ids.append(call_id)
            try:
                response = await self._gateway.generate(
                    model_request,
                    AgentTurn,
                    ModelCallContext(
                        model_call_id=call_id,
                        tenant_id=messages_ref.tenant_id,
                        run_id=messages_ref.run_id,
                        work_item_id=request.work_item_id,
                        reservation_usd=self._reservation_usd,
                    ),
                )
            except ModelCompletionUnknownError:
                return self._failed(
                    request,
                    tuple(call_ids),
                    ErrorCode.MODEL_COMPLETION_UNKNOWN,
                    "模型调用结果未知，需要人工决定是否创建新 attempt",
                    retryable=False,
                )
            except BudgetExceededError:
                return self._failed(
                    request,
                    tuple(call_ids),
                    ErrorCode.BUDGET_EXCEEDED,
                    "模型预算不足",
                    retryable=False,
                )
            except ModelOutputInvalidError as exc:
                consecutive_format_errors += 1
                messages.append(
                    {
                        "role": "user",
                        "content": f"上一轮结构化输出无效，请严格按 Schema 重试：{exc}",
                    }
                )
                if consecutive_format_errors < 2:
                    continue
                return self._failed(
                    request,
                    tuple(call_ids),
                    ErrorCode.MODEL_OUTPUT_INVALID,
                    "连续两次模型输出无效",
                )
            except ModelProviderError as exc:
                return self._failed(
                    request,
                    tuple(call_ids),
                    ErrorCode.MODEL_RATE_LIMITED,
                    str(exc),
                    retryable=True,
                )

            turn = response.output
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.model_dump(mode="json"),
                }
            )
            try:
                observation = await self._tools.execute(
                    role=request.role,
                    allowed_tools=request.allowed_tools,
                    name=turn.tool,
                    arguments=turn.arguments,
                    context=tool_context,
                )
            except ToolInputError as exc:
                consecutive_format_errors += 1
                messages.append({"role": "tool", "content": str(exc)})
                if consecutive_format_errors < 2:
                    continue
                return self._failed(
                    request,
                    tuple(call_ids),
                    ErrorCode.MODEL_OUTPUT_INVALID,
                    "连续两次工具参数无效",
                )
            except ToolNotAllowedError as exc:
                return self._failed(
                    request,
                    tuple(call_ids),
                    ErrorCode.POLICY_DENIED,
                    str(exc),
                )

            consecutive_format_errors = 0
            messages.append(
                {
                    "role": "tool",
                    "content": observation.model_dump(mode="json"),
                }
            )
            if turn.tool == "finish":
                try:
                    finished = FinishRequest.model_validate(observation)
                except ValidationError as exc:
                    return self._failed(
                        request,
                        tuple(call_ids),
                        ErrorCode.MODEL_OUTPUT_INVALID,
                        f"finish 结果无效: {exc}",
                    )
                await self._save_trajectory(request, messages)
                return AgentExecutionResult(
                    work_item_id=request.work_item_id,
                    status=finished.status,
                    output_refs=finished.output_refs,
                    model_call_ids=tuple(call_ids),
                    error=finished.error,
                )

        return self._failed(
            request,
            tuple(call_ids),
            ErrorCode.BUDGET_EXCEEDED,
            "Agent 已达到步骤或模型调用上限",
        )

    async def _load_inputs(self, request: AgentExecutionRequest) -> list[dict[str, object]]:
        messages: list[dict[str, object]] = []
        for ref in request.input_refs:
            content = await self._artifact_store.get_bytes(
                ref,
                ArtifactCaller(
                    tenant_id=ref.tenant_id,
                    run_id=ref.run_id,
                    # Activity/context builder 已按当前 WorkItem 选择精确 input_refs；
                    # 这里是可信服务读取后把内容交给模型，不向 Agent 暴露 Store 凭据。
                    role=None,
                    service="agent-context-builder",
                ),
            )
            messages.append(
                {
                    "role": "user",
                    "content": {
                        "artifact_id": str(ref.artifact_id),
                        "kind": ref.kind.value,
                        "content": content.decode("utf-8", errors="replace"),
                    },
                }
            )
        return messages

    async def _save_trajectory(
        self, request: AgentExecutionRequest, messages: list[dict[str, object]]
    ) -> ArtifactRef:
        first = request.input_refs[0]
        return await self._artifact_store.put_bytes(
            ArtifactKind.TRAJECTORY,
            json.dumps({"messages": messages}, ensure_ascii=False, sort_keys=True).encode(),
            ArtifactMetadata(
                tenant_id=first.tenant_id,
                run_id=first.run_id,
                base_revision=first.base_revision,
                schema_version="1",
                input_artifact_ids=tuple(ref.artifact_id for ref in request.input_refs),
            ),
        )

    @staticmethod
    def _validate_context(request: AgentExecutionRequest, context: ToolContext) -> None:
        if not request.input_refs:
            raise ValueError("Agent 至少需要一个输入 Artifact")
        first = request.input_refs[0]
        if any(
            ref.tenant_id != first.tenant_id or ref.run_id != first.run_id
            for ref in request.input_refs
        ):
            raise ValueError("Agent 输入 Artifact 必须属于同一 tenant/run")
        if (
            context.tenant_id != first.tenant_id
            or context.run_id != first.run_id
            or context.work_item_id != request.work_item_id
        ):
            raise ValueError("ToolContext 与 AgentExecutionRequest 不匹配")

    @staticmethod
    def _failed(
        request: AgentExecutionRequest,
        call_ids: tuple[UUID, ...],
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> AgentExecutionResult:
        return AgentExecutionResult(
            work_item_id=request.work_item_id,
            status="failed",
            output_refs=(),
            model_call_ids=call_ids,
            error=ErrorInfo(
                code=code,
                message=message,
                retryable=retryable,
                source="agent-loop",
            ),
        )


def _load_prompt(role: AgentRole) -> str:
    return (
        files("repopilot.agents.prompts")
        .joinpath(f"{role.value}_v1.md")
        .read_text(encoding="utf-8")
    )

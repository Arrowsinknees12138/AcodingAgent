"""模型供应商协议、逻辑调用键与带预算的统一 Gateway。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from decimal import Decimal
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, Field

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef


class ModelRequest(StrictModel):
    logical_call_key: str
    model: str
    system_prompt: str
    messages_ref: ArtifactRef
    tool_schema_ref: ArtifactRef | None
    temperature: float = Field(ge=0, le=2)
    top_p: float = Field(gt=0, le=1)
    max_output_tokens: int = Field(gt=0)


class ModelUsage(StrictModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)


class ModelUsageTotals(StrictModel):
    model_calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: Decimal = Field(ge=0)
    unknown_calls: int = Field(ge=0)


class ModelUsageReader(Protocol):
    async def get_run_usage(self, *, run_id: UUID, tenant_id: UUID) -> ModelUsageTotals: ...


class ModelResponse[T: BaseModel](StrictModel):
    output: T
    usage: ModelUsage
    provider_request_id: str | None
    raw_response_ref: ArtifactRef


class StructuredModelProvider(Protocol):
    name: str

    async def generate[T: BaseModel](
        self, request: ModelRequest, output_type: type[T]
    ) -> ModelResponse[T]: ...


class ModelProviderError(RuntimeError):
    """供应商明确失败；预算预留可以安全释放。"""


class ModelCompletionUnknownError(ModelProviderError):
    """请求可能已完成但响应未知；预留不能自动释放。"""


class ModelOutputInvalidError(ModelProviderError):
    """供应商已响应，但内容不符合要求的结构。"""

    def __init__(
        self,
        message: str,
        *,
        usage: ModelUsage,
        raw_response_ref: ArtifactRef,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.raw_response_ref = raw_response_ref


class ModelTemporarilyUnavailableError(ModelProviderError):
    """限流、连接失败或 5xx 在重试耗尽后仍未恢复。"""


class ModelCallReplayError(ModelCompletionUnknownError):
    """逻辑调用键已存在，拒绝无法证明安全的重复供应商调用。"""


class BudgetExceededError(RuntimeError):
    pass


class ModelCallContext(StrictModel):
    model_call_id: UUID
    tenant_id: UUID
    run_id: UUID
    work_item_id: UUID | None
    reservation_usd: Decimal = Field(gt=0)


class ModelCallReservation(StrictModel):
    model_call_id: UUID
    logical_call_key: str
    status: Literal["RESERVED", "SETTLED", "RELEASED", "UNKNOWN"]
    created: bool


class ModelBudgetStore(Protocol):
    async def reserve(
        self,
        *,
        context: ModelCallContext,
        logical_call_key: str,
        provider: str,
        model: str,
    ) -> ModelCallReservation: ...

    async def settle(
        self, *, context: ModelCallContext, usage: ModelUsage, response_ref: ArtifactRef
    ) -> None: ...

    async def release(self, model_call_id: UUID) -> None: ...

    async def mark_unknown(self, model_call_id: UUID) -> None: ...


class ModelBudgetInitializer(Protocol):
    async def create_budget(
        self,
        *,
        run_id: UUID,
        tenant_id: UUID,
        max_cost_usd: Decimal,
        max_model_calls: int,
    ) -> None: ...


def build_logical_call_key(
    *,
    tenant_id: UUID,
    work_item_id: UUID,
    attempt: int,
    provider: str,
    model: str,
    model_parameters: dict[str, object],
    prompt_version: str,
    tool_schema_version: str,
    ordered_input_artifact_hashes: tuple[str, ...],
    policy_version: str,
) -> str:
    """按第 11.2 节生成稳定、与字典插入顺序无关的调用键。"""
    payload = {
        "tenant_id": str(tenant_id),
        "work_item_id": str(work_item_id),
        "attempt": attempt,
        "provider": provider,
        "model": model,
        "model_parameters": model_parameters,
        "prompt_version": prompt_version,
        "tool_schema_version": tool_schema_version,
        "ordered_input_artifact_hashes": ordered_input_artifact_hashes,
        "policy_version": policy_version,
    }
    normalized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(normalized.encode()).hexdigest()


class BudgetedModelGateway:
    """把预算状态转换与供应商调用收口在一个可测试边界中。"""

    def __init__(self, provider: StructuredModelProvider, budget_store: ModelBudgetStore) -> None:
        self.provider = provider
        self._budget_store = budget_store

    async def generate[T: BaseModel](
        self, request: ModelRequest, output_type: type[T], context: ModelCallContext
    ) -> ModelResponse[T]:
        reservation = await self._budget_store.reserve(
            context=context,
            logical_call_key=request.logical_call_key,
            provider=self.provider.name,
            model=request.model,
        )
        if not reservation.created:
            raise ModelCallReplayError(
                f"逻辑调用 {request.logical_call_key} 已存在，拒绝重复调用模型"
            )
        try:
            response = await self.provider.generate(request, output_type)
        except asyncio.CancelledError:
            await self._budget_store.mark_unknown(context.model_call_id)
            raise
        except ModelCompletionUnknownError:
            await self._budget_store.mark_unknown(context.model_call_id)
            raise
        except ModelOutputInvalidError as exc:
            await self._settle_or_mark_unknown(
                context=context,
                usage=exc.usage,
                response_ref=exc.raw_response_ref,
            )
            raise
        except Exception:
            await self._budget_store.release(context.model_call_id)
            raise
        await self._settle_or_mark_unknown(
            context=context,
            usage=response.usage,
            response_ref=response.raw_response_ref,
        )
        return response

    async def _settle_or_mark_unknown(
        self,
        *,
        context: ModelCallContext,
        usage: ModelUsage,
        response_ref: ArtifactRef,
    ) -> None:
        try:
            await self._budget_store.settle(
                context=context,
                usage=usage,
                response_ref=response_ref,
            )
        except Exception as exc:
            await self._budget_store.mark_unknown(context.model_call_id)
            raise ModelCompletionUnknownError("模型响应已持久化，但预算结算失败") from exc

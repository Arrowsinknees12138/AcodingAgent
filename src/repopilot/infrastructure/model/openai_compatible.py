"""基于 HTTP 的 OpenAI-compatible structured-output Provider。"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import httpx
from pydantic import BaseModel, ValidationError

from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import ArtifactKind
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.model_gateway import (
    ModelCompletionUnknownError,
    ModelOutputInvalidError,
    ModelProviderError,
    ModelRequest,
    ModelResponse,
    ModelTemporarilyUnavailableError,
    ModelUsage,
)


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        artifact_store: ArtifactStore,
        input_usd_per_million_tokens: Decimal = Decimal("0"),
        output_usd_per_million_tokens: Decimal = Decimal("0"),
        client: httpx.AsyncClient | None = None,
        retry_base_seconds: float = 1.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._artifact_store = artifact_store
        self._input_rate = input_usd_per_million_tokens
        self._output_rate = output_usd_per_million_tokens
        self._client = client
        self._retry_base_seconds = retry_base_seconds

    async def generate[T: BaseModel](
        self, request: ModelRequest, output_type: type[T]
    ) -> ModelResponse[T]:
        messages = await self._load_messages(request)
        payload = {
            "model": request.model,
            "messages": [{"role": "system", "content": request.system_prompt}, *messages],
            "temperature": request.temperature,
            "top_p": request.top_p,
            "max_tokens": request.max_output_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": output_type.__name__,
                    "strict": True,
                    "schema": output_type.model_json_schema(),
                },
            },
        }
        raw = await self._post_with_retry(payload)
        usage = self._parse_usage(raw)
        try:
            raw_ref = await self._save_raw_response(request, raw)
        except Exception as exc:
            raise ModelCompletionUnknownError("模型已返回，但原始响应 Artifact 持久化失败") from exc
        try:
            content = raw["choices"][0]["message"]["content"]  # type: ignore[index]
            encoded = content if isinstance(content, str) else json.dumps(content)
            output = output_type.model_validate_json(encoded)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError) as exc:
            raise ModelOutputInvalidError(
                "模型响应不符合约定的 JSON Schema",
                usage=usage,
                raw_response_ref=raw_ref,
            ) from exc
        return ModelResponse[T](
            output=output,
            usage=usage,
            provider_request_id=_optional_string(raw.get("id")),
            raw_response_ref=raw_ref,
        )

    def _parse_usage(self, raw: dict[str, object]) -> ModelUsage:
        usage_data = raw.get("usage", {})
        if not isinstance(usage_data, dict):
            usage_data = {}
        try:
            input_tokens = int(usage_data.get("prompt_tokens", 0))
            output_tokens = int(usage_data.get("completion_tokens", 0))
        except (TypeError, ValueError) as exc:
            raise ModelProviderError("模型 usage 字段无效") from exc
        cost = (
            Decimal(input_tokens) * self._input_rate + Decimal(output_tokens) * self._output_rate
        ) / Decimal(1_000_000)
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost,
        )

    async def _save_raw_response(
        self, request: ModelRequest, raw: dict[str, object]
    ) -> ArtifactRef:
        return await self._artifact_store.put_bytes(
            ArtifactKind.MODEL_RESPONSE,
            json.dumps(raw, sort_keys=True, ensure_ascii=False).encode(),
            ArtifactMetadata(
                tenant_id=request.messages_ref.tenant_id,
                run_id=request.messages_ref.run_id,
                base_revision=request.messages_ref.base_revision,
                schema_version="1",
                input_artifact_ids=(request.messages_ref.artifact_id,),
            ),
        )

    async def _load_messages(self, request: ModelRequest) -> list[dict[str, object]]:
        content = await self._artifact_store.get_bytes(
            request.messages_ref,
            ArtifactCaller(
                tenant_id=request.messages_ref.tenant_id,
                run_id=request.messages_ref.run_id,
                role=None,
                service="model-gateway",
            ),
        )
        try:
            decoded = json.loads(content)
            messages = decoded["messages"] if isinstance(decoded, dict) else decoded
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise ModelProviderError("messages Artifact 不是合法 JSON") from exc
        if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
            raise ModelProviderError("messages Artifact 必须包含 JSON message 数组")
        return messages

    async def _post_with_retry(self, payload: dict[str, object]) -> dict[str, object]:
        headers = {"Authorization": f"Bearer {self._api_key}"}
        for attempt in range(3):
            try:
                if self._client is None:
                    async with httpx.AsyncClient(timeout=60) as client:
                        response = await client.post(
                            f"{self._base_url}/chat/completions", json=payload, headers=headers
                        )
                else:
                    response = await self._client.post(
                        f"{self._base_url}/chat/completions", json=payload, headers=headers
                    )
            except httpx.ReadTimeout as exc:
                raise ModelCompletionUnknownError("读取模型响应超时，完成状态未知") from exc
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt == 2:
                    raise ModelTemporarilyUnavailableError("连接模型供应商失败") from exc
                await self._backoff(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise ModelTemporarilyUnavailableError(
                        f"模型供应商暂时不可用: HTTP {response.status_code}"
                    )
                await self._backoff(attempt)
                continue
            if response.is_error:
                raise ModelProviderError(f"模型供应商拒绝请求: HTTP {response.status_code}")
            try:
                body = response.json()
            except ValueError as exc:
                raise ModelProviderError("模型供应商返回了非 JSON 响应") from exc
            if not isinstance(body, dict):
                raise ModelProviderError("模型供应商响应根节点必须是对象")
            return body
        raise AssertionError("retry loop 必须返回或抛出异常")

    async def _backoff(self, attempt: int) -> None:
        await asyncio.sleep(min(self._retry_base_seconds * (2**attempt), 30))


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None

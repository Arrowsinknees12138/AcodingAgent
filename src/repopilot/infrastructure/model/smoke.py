"""Small, billable smoke test for the configured structured-output endpoint."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import httpx

from repopilot.config import Settings, get_settings
from repopilot.domain import StrictModel
from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    CleanupResult,
    build_object_key,
)
from repopilot.domain.enums import ArtifactKind
from repopilot.infrastructure.model.openai_compatible import OpenAICompatibleProvider
from repopilot.services.model_gateway import ModelRequest


class ModelSmokeOutput(StrictModel):
    status: Literal["ok"]
    message: str


class ModelSmokeResult(StrictModel):
    provider: str
    model: str
    structured_output_mode: Literal["json_schema", "json_object"]
    provider_request_id: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: str
    output: ModelSmokeOutput


class _EphemeralArtifactStore:
    """Keep smoke-test prompts/responses in memory and never print their contents."""

    def __init__(self) -> None:
        self._content: dict[UUID, bytes] = {}

    async def put_bytes(
        self, kind: ArtifactKind, content: bytes, metadata: ArtifactMetadata
    ) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        ref = ArtifactRef(
            artifact_id=uuid4(),
            run_id=metadata.run_id,
            tenant_id=metadata.tenant_id,
            kind=kind,
            schema_version=metadata.schema_version,
            object_key=build_object_key(metadata.tenant_id, metadata.run_id, kind, digest),
            sha256=digest,
            size_bytes=len(content),
            base_revision=metadata.base_revision,
            input_artifact_ids=metadata.input_artifact_ids,
            created_at=datetime.now(UTC),
        )
        self._content[ref.artifact_id] = content
        return ref

    async def get_bytes(self, ref: ArtifactRef, caller: ArtifactCaller) -> bytes:
        if caller.tenant_id != ref.tenant_id or caller.run_id != ref.run_id:
            raise PermissionError("smoke artifact scope mismatch")
        return self._content[ref.artifact_id]

    async def delete_run(self, tenant_id: UUID, run_id: UUID) -> CleanupResult:
        del tenant_id, run_id
        deleted = len(self._content)
        self._content.clear()
        return CleanupResult(deleted_objects=deleted, failed_object_keys=())


async def run_model_smoke(
    settings: Settings, *, client: httpx.AsyncClient | None = None
) -> ModelSmokeResult:
    if not settings.model_base_url:
        raise ValueError("REPOPILOT_MODEL_BASE_URL 未配置")
    if settings.model_api_key is None or not settings.model_api_key.get_secret_value():
        raise ValueError("REPOPILOT_MODEL_API_KEY 未配置")

    store = _EphemeralArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    messages_ref = await store.put_bytes(
        ArtifactKind.TRAJECTORY,
        json.dumps(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Return status ok and a brief connectivity confirmation.",
                    }
                ]
            }
        ).encode(),
        ArtifactMetadata(
            tenant_id=tenant_id,
            run_id=run_id,
            base_revision="0" * 40,
            schema_version="1",
        ),
    )
    provider = OpenAICompatibleProvider(
        base_url=settings.model_base_url,
        api_key=settings.model_api_key.get_secret_value(),
        artifact_store=store,
        input_usd_per_million_tokens=settings.model_input_usd_per_million_tokens,
        output_usd_per_million_tokens=settings.model_output_usd_per_million_tokens,
        structured_output_mode=settings.model_structured_output_mode,
        client=client,
    )
    response = await provider.generate(
        ModelRequest(
            logical_call_key="model-smoke",
            model=settings.model_name,
            system_prompt="This is a connectivity test. Return only the requested JSON object.",
            messages_ref=messages_ref,
            tool_schema_ref=None,
            temperature=0,
            top_p=1,
            max_output_tokens=64,
        ),
        ModelSmokeOutput,
    )
    return ModelSmokeResult(
        provider=provider.name,
        model=settings.model_name,
        structured_output_mode=settings.model_structured_output_mode,
        provider_request_id=response.provider_request_id,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cost_usd=str(response.usage.cost_usd),
        output=response.output,
    )


def main() -> None:
    result = asyncio.run(run_model_smoke(get_settings()))
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()

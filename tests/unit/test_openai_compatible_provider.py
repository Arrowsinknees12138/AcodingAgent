"""OpenAI-compatible Provider 的 HTTP/JSON Schema 契约测试。"""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import httpx

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.enums import ArtifactKind
from repopilot.infrastructure.model.openai_compatible import OpenAICompatibleProvider
from repopilot.services.model_gateway import ModelRequest
from tests.fakes import MemoryArtifactStore


class _StructuredOutput(StrictModel):
    names: tuple[str, ...]


async def test_provider_sends_json_schema_and_accepts_json_arrays() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    messages_ref = await store.put_bytes(
        ArtifactKind.TRAJECTORY,
        b'{"messages":[{"role":"user","content":"plan"}]}',
        ArtifactMetadata(
            tenant_id=tenant_id,
            run_id=run_id,
            base_revision="a" * 40,
            schema_version="1",
        ),
    )
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "req-1",
                "choices": [{"message": {"content": '{"names":["one","two"]}'}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50},
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="https://models.example/v1",
        api_key="secret",
        artifact_store=store,
        input_usd_per_million_tokens=Decimal("1"),
        output_usd_per_million_tokens=Decimal("2"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_base_seconds=0,
    )
    response = await provider.generate(
        ModelRequest(
            logical_call_key="b" * 64,
            model="test-model",
            system_prompt="system",
            messages_ref=messages_ref,
            tool_schema_ref=None,
            temperature=0,
            top_p=1,
            max_output_tokens=100,
        ),
        _StructuredOutput,
    )

    assert response.output.names == ("one", "two")
    assert response.usage.cost_usd == Decimal("0.0002")
    assert seen_payload["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "_StructuredOutput",
            "strict": True,
            "schema": _StructuredOutput.model_json_schema(),
        },
    }


async def test_provider_retries_rate_limit_then_succeeds() -> None:
    store = MemoryArtifactStore()
    messages_ref = await store.put_bytes(
        ArtifactKind.TRAJECTORY,
        b"[]",
        ArtifactMetadata(
            tenant_id=uuid4(),
            run_id=uuid4(),
            base_revision="a" * 40,
            schema_version="1",
        ),
    )
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        del request
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"names":[]}'}}],
                "usage": {},
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="https://models.example/v1",
        api_key="secret",
        artifact_store=store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        retry_base_seconds=0,
    )
    await provider.generate(
        ModelRequest(
            logical_call_key="b" * 64,
            model="test-model",
            system_prompt="system",
            messages_ref=messages_ref,
            tool_schema_ref=None,
            temperature=0,
            top_p=1,
            max_output_tokens=100,
        ),
        _StructuredOutput,
    )
    assert attempts == 2


async def test_provider_supports_json_object_compatibility_mode() -> None:
    store = MemoryArtifactStore()
    messages_ref = await store.put_bytes(
        ArtifactKind.TRAJECTORY,
        b'[{"role":"user","content":"return JSON"}]',
        ArtifactMetadata(
            tenant_id=uuid4(),
            run_id=uuid4(),
            base_revision="a" * 40,
            schema_version="1",
        ),
    )
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"names":[]}'}}],
                "usage": {},
            },
        )

    provider = OpenAICompatibleProvider(
        base_url="https://models.example/v1",
        api_key="secret",
        artifact_store=store,
        structured_output_mode="json_object",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await provider.generate(
        ModelRequest(
            logical_call_key="b" * 64,
            model="test-model",
            system_prompt="system",
            messages_ref=messages_ref,
            tool_schema_ref=None,
            temperature=0,
            top_p=1,
            max_output_tokens=100,
        ),
        _StructuredOutput,
    )

    assert seen_payload["response_format"] == {"type": "json_object"}
    messages = seen_payload["messages"]
    assert isinstance(messages, list)
    first_message = messages[0]
    assert isinstance(first_message, dict)
    assert "JSON Schema" in str(first_message["content"])


async def test_provider_serializes_object_content_without_changing_other_content() -> None:
    store = MemoryArtifactStore()
    object_content = {"task": "修复加法", "constraints": {"new_dependencies": False}}
    content_parts = [{"type": "text", "text": "keep this content-part array"}]
    messages_ref = await store.put_bytes(
        ArtifactKind.TRAJECTORY,
        json.dumps(
            {
                "messages": [
                    {"role": "user", "content": object_content},
                    {"role": "user", "content": "plain text"},
                    {"role": "user", "content": content_parts},
                ]
            },
            ensure_ascii=False,
        ).encode(),
        ArtifactMetadata(
            tenant_id=uuid4(),
            run_id=uuid4(),
            base_revision="a" * 40,
            schema_version="1",
        ),
    )
    seen_payload: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_payload.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"names":[]}'}}], "usage": {}},
        )

    provider = OpenAICompatibleProvider(
        base_url="https://models.example/v1",
        api_key="secret",
        artifact_store=store,
        structured_output_mode="json_object",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    await provider.generate(
        ModelRequest(
            logical_call_key="b" * 64,
            model="test-model",
            system_prompt="system",
            messages_ref=messages_ref,
            tool_schema_ref=None,
            temperature=0,
            top_p=1,
            max_output_tokens=100,
        ),
        _StructuredOutput,
    )

    sent_messages = seen_payload["messages"]
    assert isinstance(sent_messages, list)
    sent_content = [message["content"] for message in sent_messages[1:]]
    assert isinstance(sent_content[0], str)
    assert json.loads(sent_content[0]) == object_content
    assert sent_content[1] == "plain text"
    assert sent_content[2] == content_parts

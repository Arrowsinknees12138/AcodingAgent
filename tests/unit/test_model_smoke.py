import json

import httpx

from repopilot.config import Settings
from repopilot.infrastructure.model.smoke import run_model_smoke


async def test_model_smoke_uses_configured_endpoint_without_exposing_key() -> None:
    seen_authorization = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_authorization
        seen_authorization = request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "id": "smoke-1",
                "choices": [{"message": {"content": '{"status":"ok","message":"connected"}'}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 5},
            },
        )

    settings = Settings(
        _env_file=None,
        model_base_url="https://models.example/v1",
        model_api_key="top-secret",
        model_name="compatible-model",
        model_structured_output_mode="json_object",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await run_model_smoke(settings, client=client)

    assert result.provider_request_id == "smoke-1"
    assert result.output.status == "ok"
    assert result.structured_output_mode == "json_object"
    assert "top-secret" not in json.dumps(result.model_dump(mode="json"))
    assert seen_authorization == "Bearer top-secret"

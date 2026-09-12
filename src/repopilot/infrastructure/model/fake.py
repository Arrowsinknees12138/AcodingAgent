"""确定性的 Fake Model Provider，用于单元测试和 E2E。"""

from __future__ import annotations

import json
from collections import deque
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.enums import ArtifactKind
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.model_gateway import (
    ModelOutputInvalidError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class FakeModelProvider:
    name = "fake"

    def __init__(
        self,
        outputs: list[BaseModel | dict[str, object]],
        artifact_store: ArtifactStore,
    ) -> None:
        self._outputs = deque(outputs)
        self._artifact_store = artifact_store
        self.requests: list[ModelRequest] = []

    async def generate[T: BaseModel](
        self, request: ModelRequest, output_type: type[T]
    ) -> ModelResponse[T]:
        self.requests.append(request)
        if not self._outputs:
            raise RuntimeError("FakeModelProvider 没有剩余的预设输出")
        raw_output = self._outputs.popleft()
        value = (
            raw_output.model_dump(mode="json") if isinstance(raw_output, BaseModel) else raw_output
        )
        usage = ModelUsage(input_tokens=10, output_tokens=5, cost_usd=Decimal("0.001"))
        raw = json.dumps(
            {"output": value, "usage": usage.model_dump(mode="json")},
            ensure_ascii=False,
            sort_keys=True,
        ).encode()
        ref = await self._artifact_store.put_bytes(
            ArtifactKind.MODEL_RESPONSE,
            raw,
            ArtifactMetadata(
                tenant_id=request.messages_ref.tenant_id,
                run_id=request.messages_ref.run_id,
                base_revision=request.messages_ref.base_revision,
                schema_version="1",
                input_artifact_ids=(request.messages_ref.artifact_id,),
            ),
        )
        try:
            output = output_type.model_validate(value)
        except ValidationError as exc:
            raise ModelOutputInvalidError(
                "Fake Model 输出不符合 Schema",
                usage=usage,
                raw_response_ref=ref,
            ) from exc
        return ModelResponse(
            output=output,
            usage=usage,
            provider_request_id=f"fake-{len(self.requests)}",
            raw_response_ref=ref,
        )

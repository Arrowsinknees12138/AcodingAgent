"""多个测试模块共用的纯内存 adapter。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import UUID, uuid4

from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    CleanupResult,
    build_object_key,
)
from repopilot.domain.enums import ArtifactKind
from repopilot.services.model_gateway import (
    ModelCallContext,
    ModelCallReservation,
    ModelUsage,
)


class MemoryArtifactStore:
    def __init__(self) -> None:
        self.content: dict[UUID, bytes] = {}
        self.refs: list[ArtifactRef] = []

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
        self.content[ref.artifact_id] = content
        self.refs.append(ref)
        return ref

    async def get_bytes(self, ref: ArtifactRef, caller: ArtifactCaller) -> bytes:
        assert caller.tenant_id == ref.tenant_id
        assert caller.run_id == ref.run_id
        return self.content[ref.artifact_id]

    async def delete_run(self, tenant_id: UUID, run_id: UUID) -> CleanupResult:
        del tenant_id, run_id
        return CleanupResult(deleted_objects=0, failed_object_keys=())


class RecordingBudgetStore:
    def __init__(self) -> None:
        self.reserved: list[UUID] = []
        self.settled: list[UUID] = []
        self.released: list[UUID] = []
        self.unknown: list[UUID] = []

    async def reserve(
        self,
        *,
        context: ModelCallContext,
        logical_call_key: str,
        provider: str,
        model: str,
    ) -> ModelCallReservation:
        del provider, model
        self.reserved.append(context.model_call_id)
        return ModelCallReservation(
            model_call_id=context.model_call_id,
            logical_call_key=logical_call_key,
            status="RESERVED",
            created=True,
        )

    async def settle(
        self,
        *,
        context: ModelCallContext,
        usage: ModelUsage,
        response_ref: ArtifactRef,
    ) -> None:
        del usage, response_ref
        self.settled.append(context.model_call_id)

    async def release(self, model_call_id: UUID) -> None:
        self.released.append(model_call_id)

    async def mark_unknown(self, model_call_id: UUID) -> None:
        self.unknown.append(model_call_id)

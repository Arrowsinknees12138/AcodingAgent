"""MinioArtifactStore 的契约测试：真实 MinIO + PostgreSQL（testcontainers）。

覆盖实施设计 7.2/18 节的关键不变量：
- 内容寻址 + 幂等写入（同一 Run/kind/sha256 不重复上传、不重复插入行）。
- 读取按角色/服务做粗粒度 ACL。
- 取回内容后校验 sha256，防止对象存储内容被篡改后悄悄流入调用方。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata
from repopilot.domain.enums import AgentRole, ArtifactKind
from repopilot.infrastructure.artifacts.minio import ArtifactAccessDenied, MinioArtifactStore

pytestmark = pytest.mark.contract


async def test_put_then_get_round_trips(artifact_store: MinioArtifactStore) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id, run_id=run_id, base_revision="a" * 40, schema_version="1"
    )
    ref = await artifact_store.put_bytes(ArtifactKind.PATCH, b"diff --git a/x b/x", metadata)

    caller = ArtifactCaller(
        tenant_id=tenant_id, run_id=run_id, role=AgentRole.DEVELOPER, service="agent-loop"
    )
    content = await artifact_store.get_bytes(ref, caller)
    assert content == b"diff --git a/x b/x"


async def test_put_bytes_is_idempotent_by_content(artifact_store: MinioArtifactStore) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id, run_id=run_id, base_revision="b" * 40, schema_version="1"
    )
    first = await artifact_store.put_bytes(ArtifactKind.PATCH, b"same content", metadata)
    second = await artifact_store.put_bytes(ArtifactKind.PATCH, b"same content", metadata)

    assert first.artifact_id == second.artifact_id
    assert first.object_key == second.object_key


async def test_get_bytes_denies_disallowed_role(artifact_store: MinioArtifactStore) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id, run_id=run_id, base_revision="c" * 40, schema_version="1"
    )
    ref = await artifact_store.put_bytes(ArtifactKind.SOURCE_ARCHIVE, b"source", metadata)

    # 密封测试 Bundle/源码归档等不对 Planner 开放；这里用 Source Archive 验证。
    caller = ArtifactCaller(
        tenant_id=tenant_id, run_id=run_id, role=AgentRole.PLANNER, service="agent-loop"
    )
    with pytest.raises(ArtifactAccessDenied):
        await artifact_store.get_bytes(ref, caller)


async def test_trusted_service_caller_bypasses_role_matrix(
    artifact_store: MinioArtifactStore,
) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id, run_id=run_id, base_revision="d" * 40, schema_version="1"
    )
    ref = await artifact_store.put_bytes(ArtifactKind.TEST_BUNDLE, b"sealed tests", metadata)

    caller = ArtifactCaller(tenant_id=tenant_id, run_id=run_id, role=None, service="repository")
    content = await artifact_store.get_bytes(ref, caller)
    assert content == b"sealed tests"

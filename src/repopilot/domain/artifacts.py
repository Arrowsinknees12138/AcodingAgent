"""Artifact 相关契约（实施设计第 7.2、13、18 节）。

设计上的核心约束：Temporal History 中只允许传 `ArtifactRef`（一个小的
索引结构），源码、diff、测试内容、模型原始回复等大字段一律先写入
Artifact Store，再把引用传给 Workflow/Activity，避免把内容塞进 History。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import field_validator, model_validator

from repopilot.domain import StrictModel
from repopilot.domain.enums import AgentRole, ArtifactKind

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FULL_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ArtifactRef(StrictModel):
    artifact_id: UUID
    run_id: UUID
    tenant_id: UUID
    kind: ArtifactKind
    schema_version: str
    object_key: str
    sha256: str
    size_bytes: int
    base_revision: str | None
    input_artifact_ids: tuple[UUID, ...]
    created_at: datetime

    @field_validator("sha256")
    @classmethod
    def _sha256_must_be_hex64(cls, value: str) -> str:
        if not _SHA256_RE.match(value):
            raise ValueError("sha256 必须是 64 位小写十六进制字符串")
        return value

    @field_validator("size_bytes")
    @classmethod
    def _size_must_be_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("size_bytes 不能为负数")
        return value

    @model_validator(mode="after")
    def _only_create_run_request_may_omit_base_revision(self) -> ArtifactRef:
        # 只有 CREATE_RUN_REQUEST 允许 base_revision=None；Ingest 固定 revision
        # 之后产生的所有其他 Artifact 必须携带完整 40 位 commit SHA（第 7.2 节）。
        if self.kind == ArtifactKind.CREATE_RUN_REQUEST:
            return self
        if self.base_revision is None or not _FULL_COMMIT_SHA_RE.match(self.base_revision):
            raise ValueError(
                f"{self.kind.value} 类型的 Artifact 必须携带完整 "
                "40 位 commit SHA 作为 base_revision"
            )
        return self


def build_object_key(tenant_id: UUID, run_id: UUID, kind: ArtifactKind, sha256: str) -> str:
    """Artifact Store 的对象 key 格式：`{tenant_id}/{run_id}/{kind}/{sha256}`。

    集中在这里定义，避免 store 实现和测试各写一份拼接逻辑导致不一致。
    """
    return f"{tenant_id}/{run_id}/{kind.value}/{sha256}"


class RepositorySnapshot(StrictModel):
    repository_url: str
    base_revision: str
    source_archive_ref: ArtifactRef
    primary_language: Literal["python"]
    python_versions: tuple[str, ...]
    dependency_manifest_paths: tuple[str, ...]
    test_config_paths: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    symbol_index_ref: ArtifactRef


class ExportedSymbol(StrictModel):
    qualified_name: str
    kind: Literal["function", "class", "constant"]
    signature: str
    change: Literal["added", "modified", "removed", "unchanged"]
    confidence: float


class ExportedInterfacePayload(StrictModel):
    work_item_id: UUID
    commit_sha: str
    symbols: tuple[ExportedSymbol, ...]
    unresolved_references: tuple[str, ...]


class ArtifactMetadata(StrictModel):
    """写入 Artifact 时附带的元数据（实施设计第 18 节）。"""

    tenant_id: UUID
    run_id: UUID
    base_revision: str | None
    schema_version: str
    input_artifact_ids: tuple[UUID, ...] = ()


class ArtifactCaller(StrictModel):
    """读取 Artifact 的调用方身份，供 ACL 授权使用。"""

    tenant_id: UUID
    run_id: UUID
    role: AgentRole | None
    service: str


class CleanupResult(StrictModel):
    deleted_objects: int
    failed_object_keys: tuple[str, ...]

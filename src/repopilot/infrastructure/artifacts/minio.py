"""ArtifactStore 的 MinIO + PostgreSQL 实现（实施设计第 17、18 节）。

写入路径：内容寻址（sha256）+ 数据库幂等判断——先查 `artifacts` 表里
是否已经有同一 `(tenant_id, run_id, kind, sha256)` 的记录，有就直接
复用，不重复上传、也不重复插入行；没有才真正写 MinIO + 插入行。

读取路径：先做粗粒度 ACL 授权（见 `services.artifact_store.check_read_access`），
再从 MinIO 取回字节并校验 sha256，防止对象存储内容被后台篡改后悄悄流入
Agent 上下文。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
from datetime import UTC, datetime
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from minio import Minio
from minio.error import S3Error
from sqlalchemy.ext.asyncio import async_sessionmaker

from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    CleanupResult,
    build_object_key,
)
from repopilot.domain.enums import ArtifactKind
from repopilot.infrastructure.db.repositories import ArtifactRepository
from repopilot.logging import get_logger
from repopilot.services.artifact_store import check_read_access

logger = get_logger(component="artifact_store")


class ArtifactAccessDenied(PermissionError):
    """调用方角色/服务无权读取该种类的 Artifact。"""


class ArtifactIntegrityError(RuntimeError):
    """从对象存储取回的内容与记录的 sha256 不一致。"""


def build_minio_client(endpoint: str, access_key: str, secret_key: str) -> Minio:
    """从 `http(s)://host:port` 形式的 endpoint 构造 minio-py 客户端。

    公开函数：composition root（Worker/API 启动入口）和测试都用它，避免
    各自重复解析 endpoint 字符串的逻辑。
    """
    parts = urlsplit(endpoint)
    secure = parts.scheme == "https"
    host = parts.netloc or parts.path  # 允许传入不带 scheme 的 host:port
    return Minio(host, access_key=access_key, secret_key=secret_key, secure=secure)


class MinioArtifactStore:
    """实现 `services.artifact_store.ArtifactStore` Protocol。"""

    def __init__(
        self,
        *,
        client: Minio,
        bucket: str,
        session_factory: async_sessionmaker,  # type: ignore[type-arg]
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._session_factory = session_factory

    async def ensure_bucket(self) -> None:
        exists = await asyncio.to_thread(self._client.bucket_exists, self._bucket)
        if not exists:
            await asyncio.to_thread(self._client.make_bucket, self._bucket)

    async def put_bytes(
        self,
        kind: ArtifactKind,
        content: bytes,
        metadata: ArtifactMetadata,
    ) -> ArtifactRef:
        sha256 = hashlib.sha256(content).hexdigest()
        object_key = build_object_key(metadata.tenant_id, metadata.run_id, kind, sha256)
        artifact_id = uuid4()
        created_at = datetime.now(UTC)

        # 先构造并校验 ArtifactRef——任何 domain 层校验失败（例如非
        # CREATE_RUN_REQUEST 却没带完整 commit SHA）都必须在触碰 MinIO/
        # 数据库之前抛出，不能先写入一半再报错。
        candidate_ref = ArtifactRef(
            artifact_id=artifact_id,
            run_id=metadata.run_id,
            tenant_id=metadata.tenant_id,
            kind=kind,
            schema_version=metadata.schema_version,
            object_key=object_key,
            sha256=sha256,
            size_bytes=len(content),
            base_revision=metadata.base_revision,
            input_artifact_ids=metadata.input_artifact_ids,
            created_at=created_at,
        )

        async with self._session_factory() as session:
            repo = ArtifactRepository(session)
            existing = await repo.find_by_content_identity(
                tenant_id=metadata.tenant_id,
                run_id=metadata.run_id,
                kind=kind.value,
                sha256=sha256,
            )
            if existing is not None:
                logger.info(
                    "artifact.put.deduplicated",
                    artifact_id=str(existing.artifact_id),
                    kind=kind.value,
                )
                return _row_to_ref(existing, metadata.input_artifact_ids)

            await asyncio.to_thread(
                self._client.put_object,
                self._bucket,
                object_key,
                data=io.BytesIO(content),
                length=len(content),
            )
            await repo.insert(
                artifact_id=artifact_id,
                tenant_id=metadata.tenant_id,
                run_id=metadata.run_id,
                kind=kind.value,
                schema_version=metadata.schema_version,
                object_key=object_key,
                sha256=sha256,
                size_bytes=len(content),
                base_revision=metadata.base_revision,
                created_at=created_at,
            )
            await session.commit()

        logger.info("artifact.put.created", artifact_id=str(artifact_id), kind=kind.value)
        return candidate_ref

    async def get_bytes(self, ref: ArtifactRef, caller: ArtifactCaller) -> bytes:
        if not check_read_access(ref.kind, caller):
            logger.info(
                "artifact.read.denied",
                artifact_id=str(ref.artifact_id),
                kind=ref.kind.value,
                role=caller.role.value if caller.role else None,
                service=caller.service,
            )
            raise ArtifactAccessDenied(
                f"{caller.service} (role={caller.role}) 无权读取 {ref.kind.value} 类型的 Artifact"
            )

        try:
            response = await asyncio.to_thread(
                self._client.get_object, self._bucket, ref.object_key
            )
            try:
                content = await asyncio.to_thread(response.read)
            finally:
                response.close()
                response.release_conn()
        except S3Error as exc:
            raise FileNotFoundError(f"Artifact 对象不存在: {ref.object_key}") from exc

        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_sha256 != ref.sha256:
            raise ArtifactIntegrityError(
                f"对象 {ref.object_key} 内容 sha256={actual_sha256} 与记录 {ref.sha256} 不一致"
            )

        logger.info("artifact.read.allowed", artifact_id=str(ref.artifact_id), kind=ref.kind.value)
        return content

    async def delete_run(self, tenant_id: UUID, run_id: UUID) -> CleanupResult:
        async with self._session_factory() as session:
            repo = ArtifactRepository(session)
            rows = await repo.list_by_run(tenant_id=tenant_id, run_id=run_id)

        deleted = 0
        failed: list[str] = []
        for row in rows:
            try:
                await asyncio.to_thread(self._client.remove_object, self._bucket, row.object_key)
                deleted += 1
            except S3Error:
                failed.append(row.object_key)
                logger.warning("artifact.cleanup.failed", object_key=row.object_key)

        return CleanupResult(deleted_objects=deleted, failed_object_keys=tuple(failed))


def _row_to_ref(row: object, input_artifact_ids: tuple[UUID, ...]) -> ArtifactRef:
    # `row` 是 infrastructure.db.models.Artifact；用 duck typing 避免在类型注解
    # 里牵扯 ORM 类，保持这个转换函数容易在不同 repository 实现之间复用。
    return ArtifactRef(
        artifact_id=row.artifact_id,  # type: ignore[attr-defined]
        run_id=row.run_id,  # type: ignore[attr-defined]
        tenant_id=row.tenant_id,  # type: ignore[attr-defined]
        kind=ArtifactKind(row.kind),  # type: ignore[attr-defined]
        schema_version=row.schema_version,  # type: ignore[attr-defined]
        object_key=row.object_key,  # type: ignore[attr-defined]
        sha256=row.sha256,  # type: ignore[attr-defined]
        size_bytes=row.size_bytes,  # type: ignore[attr-defined]
        base_revision=row.base_revision,  # type: ignore[attr-defined]
        input_artifact_ids=input_artifact_ids,
        created_at=row.created_at,  # type: ignore[attr-defined]
    )

"""Repository 层：把 SQLAlchemy 查询封装成小方法，供 services/infrastructure
的其他模块调用，避免 ORM 查询逻辑散落在业务代码里。

Milestone 2 只实现 Artifact Store 需要的最小子集（按内容身份幂等查找/插入）；
`run_budgets`、`model_calls` 等表的 repository 方法会在 Milestone 6（预算
事务）时补充，避免提前写没有调用方的代码。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from repopilot.infrastructure.db.models import Artifact


class ArtifactRepository:
    """`artifacts` 表的读写封装。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_content_identity(
        self, *, tenant_id: uuid.UUID, run_id: uuid.UUID, kind: str, sha256: str
    ) -> Artifact | None:
        """按 (tenant_id, run_id, kind, sha256) 查找，用于幂等写入判断。

        对应实施设计第 7.2 节："同一 Run、kind、sha256 重复写入返回同一 Artifact"。
        """
        stmt = select(Artifact).where(
            Artifact.tenant_id == tenant_id,
            Artifact.run_id == run_id,
            Artifact.kind == kind,
            Artifact.sha256 == sha256,
        )
        result: Artifact | None = await self._session.scalar(stmt)
        return result

    async def get_by_id(self, artifact_id: uuid.UUID) -> Artifact | None:
        return await self._session.get(Artifact, artifact_id)

    async def insert(
        self,
        *,
        artifact_id: uuid.UUID,
        tenant_id: uuid.UUID,
        run_id: uuid.UUID,
        kind: str,
        schema_version: str,
        object_key: str,
        sha256: str,
        size_bytes: int,
        base_revision: str | None,
        created_at: datetime,
    ) -> Artifact:
        row = Artifact(
            artifact_id=artifact_id,
            tenant_id=tenant_id,
            run_id=run_id,
            kind=kind,
            schema_version=schema_version,
            object_key=object_key,
            sha256=sha256,
            size_bytes=size_bytes,
            base_revision=base_revision,
            created_at=created_at,
            expires_at=None,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_by_run(self, *, tenant_id: uuid.UUID, run_id: uuid.UUID) -> list[Artifact]:
        stmt = select(Artifact).where(Artifact.tenant_id == tenant_id, Artifact.run_id == run_id)
        result = await self._session.scalars(stmt)
        return list(result.all())

"""PostgreSQL + Artifact Store + Temporal 组成的 Run 控制面实现。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import ArtifactKind, RunStatus
from repopilot.domain.tasks import CreateRunRequest
from repopilot.infrastructure.db.model_budget import PostgresModelBudgetStore
from repopilot.infrastructure.db.models import Artifact, AuditEvent, IdempotencyKey, RunProjection
from repopilot.infrastructure.temporal.client import (
    start_code_repair_workflow,
    workflow_id_for,
)
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.model_gateway import ModelBudgetInitializer
from repopilot.services.run_control import (
    ArtifactDownload,
    ArtifactView,
    IdempotencyConflictError,
    RunControl,
    RunEventView,
    RunNotFoundError,
    RunUnavailableError,
    RunView,
)
from repopilot.services.run_projection import RunProjectionStore
from repopilot.workflows.code_repair import CodeRepairWorkflowInput
from repopilot.workflows.updates import ApprovalRequest


class PostgresTemporalRunControl(RunControl):
    def __init__(
        self,
        *,
        tenant_id: UUID,
        session_factory: async_sessionmaker[AsyncSession],
        artifact_store: ArtifactStore,
        projection_store: RunProjectionStore,
        temporal_client: Client,
        model_budget_initializer: ModelBudgetInitializer | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._session_factory = session_factory
        self._artifact_store = artifact_store
        self._projection_store = projection_store
        self._temporal = temporal_client
        self._model_budget_initializer = model_budget_initializer or PostgresModelBudgetStore(
            session_factory
        )

    async def create(self, request: CreateRunRequest, *, idempotency_key: str) -> RunView:
        request_bytes = request.model_dump_json().encode("utf-8")
        request_hash = hashlib.sha256(request_bytes).hexdigest()
        run_id = uuid5(self._tenant_id, idempotency_key)
        record = await self._reserve_idempotency_key(
            key=idempotency_key,
            request_hash=request_hash,
            run_id=run_id,
        )
        if record.status == "STARTED":
            return await self.get(record.run_id)

        request_ref = await self._artifact_store.put_bytes(
            ArtifactKind.CREATE_RUN_REQUEST,
            request_bytes,
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=run_id,
                base_revision=None,
                schema_version="1",
            ),
        )
        workflow_input = CodeRepairWorkflowInput(
            run_id=run_id,
            tenant_id=self._tenant_id,
            create_request_ref=request_ref,
            risk_level=request.risk_level,
            approval_policy=request.approval_policy,
            developer_agent_mode=True,
            planner_agent_mode=True,
            investigator_agent_mode=True,
            scope_expansion_enabled=True,
        )
        try:
            await self._model_budget_initializer.create_budget(
                run_id=run_id,
                tenant_id=self._tenant_id,
                max_cost_usd=request.budget.max_cost_usd,
                max_model_calls=request.budget.max_model_calls,
            )
        except Exception as exc:
            await self._set_idempotency_status(idempotency_key, "FAILED", request_ref.artifact_id)
            raise RunUnavailableError("模型预算暂时无法初始化") from exc
        try:
            await start_code_repair_workflow(self._temporal, workflow_input)
        except WorkflowAlreadyStartedError:
            pass
        except Exception as exc:
            await self._set_idempotency_status(idempotency_key, "FAILED", request_ref.artifact_id)
            raise RunUnavailableError("Temporal 暂时无法启动 Run") from exc
        await self._set_idempotency_status(idempotency_key, "STARTED", request_ref.artifact_id)
        return RunView(
            run_id=run_id,
            workflow_id=workflow_id_for(self._tenant_id, run_id),
            status=RunStatus.QUEUED,
        )

    async def get(self, run_id: UUID) -> RunView:
        await self._require_owned_run(run_id)
        projection = await self._projection_store.get(run_id)
        if projection is None:
            return RunView(
                run_id=run_id,
                workflow_id=workflow_id_for(self._tenant_id, run_id),
                status=RunStatus.QUEUED,
            )
        return RunView(
            run_id=run_id,
            workflow_id=projection.workflow_id,
            status=projection.status,
            base_revision=projection.base_revision,
            plan_version=projection.plan_version,
            model_calls=projection.model_calls,
        )

    async def list_runs(self, limit: int = 50) -> tuple[RunView, ...]:
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(RunProjection)
                .where(RunProjection.tenant_id == self._tenant_id)
                .order_by(RunProjection.created_at.desc(), RunProjection.run_id.desc())
                .limit(limit)
            )
        return tuple(
            RunView(
                run_id=row.run_id,
                workflow_id=row.workflow_id,
                status=RunStatus(row.status),
                base_revision=row.base_revision,
                plan_version=row.plan_version,
                model_calls=row.model_calls,
            )
            for row in rows
        )

    async def approve(self, run_id: UUID, approval: ApprovalRequest) -> None:
        await self._require_owned_run(run_id)
        handle = self._temporal.get_workflow_handle(workflow_id_for(self._tenant_id, run_id))
        try:
            await handle.execute_update("submit_approval", approval)
        except Exception as exc:
            raise RunUnavailableError("审批未被 Workflow 接受") from exc

    async def cancel(self, run_id: UUID) -> None:
        await self._require_owned_run(run_id)
        handle = self._temporal.get_workflow_handle(workflow_id_for(self._tenant_id, run_id))
        try:
            await handle.cancel()
        except Exception as exc:
            raise RunUnavailableError("取消请求未送达 Workflow") from exc

    async def list_events(self, run_id: UUID) -> tuple[RunEventView, ...]:
        await self._require_owned_run(run_id)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.tenant_id == self._tenant_id,
                    AuditEvent.run_id == run_id,
                )
                .order_by(AuditEvent.created_at, AuditEvent.event_id)
            )
        return tuple(
            RunEventView(
                event_id=row.event_id,
                event_type=row.event_type,
                actor_type=row.actor_type,
                actor_id=row.actor_id,
                payload=row.payload,
                created_at=row.created_at,
            )
            for row in rows
        )

    async def list_artifacts(self, run_id: UUID) -> tuple[ArtifactView, ...]:
        await self._require_owned_run(run_id)
        async with self._session_factory() as session:
            rows = await session.scalars(
                select(Artifact)
                .where(Artifact.tenant_id == self._tenant_id, Artifact.run_id == run_id)
                .order_by(Artifact.created_at, Artifact.artifact_id)
            )
        return tuple(self._artifact_view(row) for row in rows)

    async def download_artifact(self, artifact_id: UUID) -> ArtifactDownload:
        async with self._session_factory() as session:
            row = await session.scalar(
                select(Artifact).where(
                    Artifact.tenant_id == self._tenant_id,
                    Artifact.artifact_id == artifact_id,
                )
            )
        if row is None:
            raise RunNotFoundError(f"Artifact {artifact_id} 不存在")
        await self._require_owned_run(row.run_id)
        view = self._artifact_view(row)
        ref = ArtifactRef(
            artifact_id=row.artifact_id,
            run_id=row.run_id,
            tenant_id=row.tenant_id,
            kind=ArtifactKind(row.kind),
            schema_version=row.schema_version,
            object_key=row.object_key,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            base_revision=row.base_revision,
            input_artifact_ids=(),
            created_at=row.created_at,
        )
        content = await self._artifact_store.get_bytes(
            ref,
            ArtifactCaller(
                tenant_id=self._tenant_id,
                run_id=row.run_id,
                role=None,
                service="api",
            ),
        )
        return ArtifactDownload(artifact=view, content=content)

    @staticmethod
    def _artifact_view(row: Artifact) -> ArtifactView:
        return ArtifactView(
            artifact_id=row.artifact_id,
            run_id=row.run_id,
            kind=row.kind,
            schema_version=row.schema_version,
            sha256=row.sha256,
            size_bytes=row.size_bytes,
            base_revision=row.base_revision,
            created_at=row.created_at,
        )

    async def _reserve_idempotency_key(
        self,
        *,
        key: str,
        request_hash: str,
        run_id: UUID,
    ) -> IdempotencyKey:
        now = datetime.now(UTC)
        statement = (
            insert(IdempotencyKey)
            .values(
                tenant_id=self._tenant_id,
                key=key,
                request_sha256=request_hash,
                run_id=run_id,
                request_artifact_id=None,
                status="PENDING",
                expires_at=now + timedelta(hours=24),
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "key"])
        )
        async with self._session_factory() as session:
            await session.execute(statement)
            await session.commit()
            record = await session.scalar(
                select(IdempotencyKey).where(
                    IdempotencyKey.tenant_id == self._tenant_id,
                    IdempotencyKey.key == key,
                )
            )
        if record is None:
            raise RunUnavailableError("无法保存幂等键")
        if record.request_sha256 != request_hash:
            raise IdempotencyConflictError("相同 Idempotency-Key 对应了不同请求体")
        return record

    async def _set_idempotency_status(
        self, key: str, status: str, request_artifact_id: UUID
    ) -> None:
        async with self._session_factory() as session:
            await session.execute(
                update(IdempotencyKey)
                .where(
                    IdempotencyKey.tenant_id == self._tenant_id,
                    IdempotencyKey.key == key,
                )
                .values(status=status, request_artifact_id=request_artifact_id)
            )
            await session.commit()

    async def _require_owned_run(self, run_id: UUID) -> None:
        async with self._session_factory() as session:
            owned = await session.scalar(
                select(IdempotencyKey.run_id).where(
                    IdempotencyKey.tenant_id == self._tenant_id,
                    IdempotencyKey.run_id == run_id,
                )
            )
        if owned is None:
            raise RunNotFoundError(f"Run {run_id} 不存在")

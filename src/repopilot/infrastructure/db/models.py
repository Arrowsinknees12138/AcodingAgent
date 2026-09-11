"""SQLAlchemy 2 Async ORM 模型（实施设计第 17.1 节）。

这些表是 Temporal 状态的查询投影 / 事务边界，**不是** Workflow 的恢复来源
（Workflow 状态永远从 Temporal History 恢复）。字段、索引和约束严格对照
设计文档表格，任何调整都应该先改文档。

一处刻意偏离文档字面表述的地方：`artifacts.base_revision` 文档写的是
"not null"，但 `ArtifactRef`（domain 层）明确允许 `CREATE_RUN_REQUEST`
类型的 Artifact 不带 base_revision。这里让该列可空，把"非 CREATE_RUN_REQUEST
必须有 base_revision"这条规则放在写入前的领域校验（ArtifactRef 的
model_validator）里执行，而不是用数据库约束表达一个依赖 kind 取值的条件
约束。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    # 所有 `datetime` 字段默认映射为 `timestamptz`（带时区）。设计文档里
    # 每一列都写的是 timestamptz，而不是裸 timestamp；应用层也统一用
    # `datetime.now(UTC)` 这种带时区的值，两边必须一致，否则 asyncpg 会在
    # 写入时直接报错（"can't subtract offset-naive and offset-aware
    # datetimes"）。
    type_annotation_map = {datetime: DateTime(timezone=True)}


class IdempotencyKey(Base):
    """API `Idempotency-Key` -> run_id 的唯一映射（第 19.2 节）。"""

    __tablename__ = "idempotency_keys"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    request_sha256: Mapped[str] = mapped_column()
    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), unique=True, nullable=False)
    request_artifact_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    status: Mapped[str] = mapped_column()  # PENDING / STARTED / FAILED
    expires_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        CheckConstraint(
            "status in ('PENDING', 'STARTED', 'FAILED')", name="ck_idempotency_key_status"
        ),
    )


class RunProjection(Base):
    """Run 状态的查询投影，供 API 快速查询（不用于恢复 Workflow）。"""

    __tablename__ = "run_projections"

    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True, nullable=False)
    workflow_id: Mapped[str] = mapped_column(unique=True)
    status: Mapped[str] = mapped_column(index=True)
    base_revision: Mapped[str | None] = mapped_column()
    plan_version: Mapped[int] = mapped_column(default=0)
    model_calls: Mapped[int] = mapped_column(default=0)
    spent_usd: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    reserved_usd: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()
    terminal_at: Mapped[datetime | None] = mapped_column()


class RunBudget(Base):
    """费用授权的事务边界；预留/结算/释放必须使用条件更新（第 17.1 节）。"""

    __tablename__ = "run_budgets"

    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True, nullable=False)
    max_cost_usd: Mapped[Decimal] = mapped_column()
    max_model_calls: Mapped[int] = mapped_column()
    spent_usd: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    reserved_usd: Mapped[Decimal] = mapped_column(default=Decimal("0"))
    settled_calls: Mapped[int] = mapped_column(default=0)
    reserved_calls: Mapped[int] = mapped_column(default=0)
    version: Mapped[int] = mapped_column()

    __table_args__ = (
        CheckConstraint("max_cost_usd > 0", name="ck_run_budgets_max_cost_positive"),
        CheckConstraint("max_model_calls > 0", name="ck_run_budgets_max_calls_positive"),
    )


class WorkItemProjection(Base):
    __tablename__ = "work_item_projections"

    work_item_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True), ForeignKey("run_projections.run_id"), index=True
    )
    owner: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column(index=True)
    attempt: Mapped[int] = mapped_column()
    input_revision: Mapped[str | None] = mapped_column()
    output_artifact_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column()


class Artifact(Base):
    __tablename__ = "artifacts"

    artifact_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    kind: Mapped[str] = mapped_column()
    schema_version: Mapped[str] = mapped_column()
    object_key: Mapped[str] = mapped_column(unique=True)
    sha256: Mapped[str] = mapped_column()
    size_bytes: Mapped[int] = mapped_column()
    base_revision: Mapped[str | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column()
    expires_at: Mapped[datetime | None] = mapped_column()

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "run_id", "kind", "sha256", name="uq_artifacts_content_identity"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_artifacts_size_non_negative"),
    )


class ModelCall(Base):
    __tablename__ = "model_calls"

    model_call_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    logical_call_key: Mapped[str] = mapped_column(unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    provider: Mapped[str] = mapped_column()
    model: Mapped[str] = mapped_column()
    status: Mapped[str] = mapped_column()  # RESERVED / SETTLED / RELEASED / UNKNOWN
    reserved_usd: Mapped[Decimal] = mapped_column()
    actual_usd: Mapped[Decimal | None] = mapped_column()
    input_tokens: Mapped[int | None] = mapped_column()
    output_tokens: Mapped[int | None] = mapped_column()
    response_artifact_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column()
    updated_at: Mapped[datetime] = mapped_column()

    __table_args__ = (
        CheckConstraint(
            "status in ('RESERVED', 'SETTLED', 'RELEASED', 'UNKNOWN')",
            name="ck_model_calls_status",
        ),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    event_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    event_type: Mapped[str] = mapped_column(index=True)
    actor_type: Mapped[str] = mapped_column()
    actor_id: Mapped[str | None] = mapped_column()
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)  # 写入前必须已脱敏
    created_at: Mapped[datetime] = mapped_column(index=True)


class PolicyDecisionRecord(Base):
    __tablename__ = "policy_decisions"

    decision_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), index=True)
    work_item_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    policy_version: Mapped[str] = mapped_column()
    action: Mapped[str] = mapped_column()
    allow: Mapped[bool] = mapped_column()
    reason_code: Mapped[str] = mapped_column()
    normalized_resource: Mapped[str] = mapped_column()
    created_at: Mapped[datetime] = mapped_column()


# 常用复合索引：按 run_id 查询某类事件是审计/调试时最常见的访问模式。
Index("ix_audit_events_run_id_event_type", AuditEvent.run_id, AuditEvent.event_type)

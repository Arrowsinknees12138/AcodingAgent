"""`CodeRepairWorkflow`（实施设计第 8 节）。

Milestone 3 范围：只验证 Temporal 编排机制本身——状态机、Update、取消、
Worker 崩溃恢复、Query——不接入真实的 ingest/plan/develop/verify/review
Activity。状态序列本身完整走完 8.6 节定义的正常路径（INGESTING ->
BASELINING -> PLANNING -> DESIGNING_TESTS -> EXECUTING -> VERIFYING ->
REVIEWING -> ...），每一步只是还没有换成真正的 Activity 调用；没有 DAG
调度、没有真实补丁，这些在 Milestone 4~7 陆续接入。

`workflows` 层不允许任何外部 I/O（第 6 节）：这里唯一的副作用是通过
`workflow.execute_activity_method` 调度 `update_projection` Activity，
写数据库的真正代码在 Activity/Infrastructure 层。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import temporalio.exceptions
from temporalio import workflow
from temporalio.common import RetryPolicy

from repopilot.activities.projections import ProjectionActivities
from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import RunStatus
from repopilot.domain.errors import ErrorInfo
from repopilot.services.run_projection import ProjectionEvent
from repopilot.workflows.transitions import validate_transition
from repopilot.workflows.updates import ApprovalRequest, validate_approval

_PROJECTION_RETRY_POLICY = RetryPolicy(maximum_attempts=5)
_PROJECTION_START_TO_CLOSE = timedelta(seconds=30)
_PROJECTION_SCHEDULE_TO_START = timedelta(seconds=30)


class CodeRepairWorkflowInput(StrictModel):
    run_id: UUID
    tenant_id: UUID
    create_request_ref: ArtifactRef
    auto_approve_low_risk: bool = True


class CodeRepairWorkflowOutput(StrictModel):
    run_id: UUID
    status: RunStatus
    final_report_ref: ArtifactRef | None
    failure: ErrorInfo | None


@workflow.defn(name="CodeRepairWorkflow")
class CodeRepairWorkflow:
    def __init__(self) -> None:
        self._status = RunStatus.QUEUED
        self._workflow_id = ""
        self._processed_approval_ids: set[UUID] = set()
        self._delivery_decision: str | None = None

    @workflow.run
    async def run(self, workflow_input: CodeRepairWorkflowInput) -> CodeRepairWorkflowOutput:
        self._workflow_id = workflow.info().workflow_id
        await self._record_projection(workflow_input)  # 记录初始 QUEUED

        try:
            # Milestone 3 依次走完 8.6 节定义的正常状态序列（INGESTING ->
            # ... -> REVIEWING），只是每一步都还没有接真正的 Activity——
            # 真正的 ingest_task/scan_repository/plan_change/design_sealed_tests/
            # develop_patch/verify_candidate/review_candidate 在 Milestone
            # 4~7 陆续替换掉这里的直接跳转。状态机本身（transitions.py）
            # 是完整、真实的，不能为了"先跑通"而抄近路创造文档里不存在的边。
            await self._transition(workflow_input, RunStatus.INGESTING)
            await self._transition(workflow_input, RunStatus.BASELINING)
            await self._transition(workflow_input, RunStatus.PLANNING)
            await self._transition(workflow_input, RunStatus.DESIGNING_TESTS)
            await self._transition(workflow_input, RunStatus.EXECUTING)
            await self._transition(workflow_input, RunStatus.VERIFYING)
            await self._transition(workflow_input, RunStatus.REVIEWING)

            if workflow_input.auto_approve_low_risk:
                return await self._finalize(workflow_input, RunStatus.SUCCEEDED)

            await self._transition(workflow_input, RunStatus.WAITING_DELIVERY_APPROVAL)
            await workflow.wait_condition(lambda: self._delivery_decision is not None)
            if self._delivery_decision == "reject":
                return await self._finalize(workflow_input, RunStatus.REJECTED)
            return await self._finalize(workflow_input, RunStatus.SUCCEEDED)
        except (asyncio.CancelledError, temporalio.exceptions.CancelledError):
            # cleanup cancellation scope 的最小版本：取消请求到达后，仍然
            # 执行一次 finalize，把状态机推进到 CANCELLED，而不是让 Workflow
            # Task 直接以异常结束、停留在一个非终态上。
            return await self._finalize(workflow_input, RunStatus.CANCELLED)

    @workflow.query
    def get_status(self) -> RunStatus:
        return self._status

    @workflow.update
    async def submit_approval(self, request: ApprovalRequest) -> None:
        validate_approval(
            request,
            current_status=self._status,
            already_processed_ids=frozenset(self._processed_approval_ids),
        )
        self._processed_approval_ids.add(request.approval_id)
        if request.kind == "delivery":
            self._delivery_decision = request.decision

    async def _transition(
        self, workflow_input: CodeRepairWorkflowInput, new_status: RunStatus
    ) -> None:
        validate_transition(self._status, new_status)
        self._status = new_status
        await self._record_projection(workflow_input)

    async def _finalize(
        self, workflow_input: CodeRepairWorkflowInput, outcome: RunStatus
    ) -> CodeRepairWorkflowOutput:
        if self._status is not RunStatus.FINALIZING:
            await self._transition(workflow_input, RunStatus.FINALIZING)
        await self._transition(workflow_input, outcome)
        return CodeRepairWorkflowOutput(
            run_id=workflow_input.run_id,
            status=outcome,
            final_report_ref=None,
            failure=None,
        )

    async def _record_projection(self, workflow_input: CodeRepairWorkflowInput) -> None:
        event = ProjectionEvent(
            run_id=workflow_input.run_id,
            tenant_id=workflow_input.tenant_id,
            workflow_id=self._workflow_id,
            status=self._status,
            occurred_at=workflow.now(),
        )
        await workflow.execute_activity_method(
            ProjectionActivities.update_projection,
            event,
            start_to_close_timeout=_PROJECTION_START_TO_CLOSE,
            schedule_to_start_timeout=_PROJECTION_SCHEDULE_TO_START,
            retry_policy=_PROJECTION_RETRY_POLICY,
        )

"""`CodeRepairWorkflow`（实施设计第 8 节）。

当前已接入 ingest、planning、sealed QA、DAG development、verification、
review、approval、cleanup 和 final report；所有结果均经过 FINALIZING 闸口。

`workflows` 层不允许任何外部 I/O（第 6 节）：这里唯一的副作用是通过
`workflow.execute_activity_method` 调度 `update_projection` Activity，
写数据库的真正代码在 Activity/Infrastructure 层。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from uuid import UUID

import temporalio.exceptions
from temporalio import workflow
from temporalio.common import RetryPolicy

from repopilot.activities.developer import DeveloperActivities, DevelopPatchInput
from repopilot.activities.finalization import BuildFinalReportInput, FinalizationActivities
from repopilot.activities.ingest import FinalizeTaskSpecInput, IngestActivities
from repopilot.activities.planning import PlanChangeInput, PlanningActivities
from repopilot.activities.projections import ProjectionActivities
from repopilot.activities.qa import DesignSealedTestsInput, QaActivities
from repopilot.activities.repository import (
    BuildDeveloperContextInput,
    IntegratePatchInput,
    RepositoryActivities,
)
from repopilot.activities.reviewer import ReviewCandidateInput, ReviewerActivities
from repopilot.activities.verification import (
    PrepareDependenciesInput,
    VerificationActivities,
    VerifyBaselineInput,
    VerifyCandidateInput,
    VerifySealedTestsInput,
)
from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import ApprovalMode, RiskLevel, RunStatus
from repopilot.domain.errors import ErrorCode, ErrorInfo
from repopilot.domain.plans import PlannedFileChange
from repopilot.domain.policies import (
    ApprovalPolicy,
    ApprovalStagePolicy,
    resolve_approval_policy,
)
from repopilot.services.run_projection import ProjectionEvent
from repopilot.services.task_queues import (
    MODEL_TASK_QUEUE,
    REPOSITORY_TASK_QUEUE,
    SANDBOX_TASK_QUEUE,
)
from repopilot.workflows.transitions import validate_transition
from repopilot.workflows.updates import ApprovalKind, ApprovalRequest, validate_approval

_PROJECTION_RETRY_POLICY = RetryPolicy(maximum_attempts=5)
_PROJECTION_START_TO_CLOSE = timedelta(seconds=30)
_PROJECTION_SCHEDULE_TO_START = timedelta(seconds=30)
_SERVICE_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
_MODEL_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=1)
_REPOSITORY_START_TO_CLOSE = timedelta(minutes=10)
_SANDBOX_START_TO_CLOSE = timedelta(minutes=15)
_MODEL_START_TO_CLOSE = timedelta(minutes=10)
_MODEL_SCHEDULE_TO_START = timedelta(minutes=2)
_CLEANUP_RETRY_POLICY = RetryPolicy(maximum_attempts=5)


class CodeRepairWorkflowInput(StrictModel):
    run_id: UUID
    tenant_id: UUID
    create_request_ref: ArtifactRef
    risk_level: RiskLevel = RiskLevel.LOW
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    # Temporal 输入会保存在不可变 History 中；保留旧字段兼容已经启动的 Run。
    # 新调用方不应再传它。False 等价于额外要求 delivery 人工审批。
    auto_approve_low_risk: bool | None = None


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
        self._approvals: dict[str, ApprovalRequest] = {}
        self._base_revision: str | None = None
        self._projection_sequence = 0
        self._started_at: datetime | None = None
        self._repository_url = ""
        self._final_revision: str | None = None
        self._patch_ref: ArtifactRef | None = None
        self._verification_ref: ArtifactRef | None = None
        self._review_ref: ArtifactRef | None = None
        self._repair_rounds = 0

    @workflow.run
    async def run(self, workflow_input: CodeRepairWorkflowInput) -> CodeRepairWorkflowOutput:
        self._workflow_id = workflow.info().workflow_id
        self._started_at = workflow.now()
        await self._record_projection(workflow_input)  # 记录初始 QUEUED

        try:
            # ingest/scan/baseline 已是真实 Activity；后半段角色 Activity 会在
            # 产物协议齐备后逐项替换状态占位。
            await self._transition(workflow_input, RunStatus.INGESTING)
            ingest_result = await workflow.execute_activity_method(
                IngestActivities.ingest_task,
                workflow_input.create_request_ref,
                task_queue=REPOSITORY_TASK_QUEUE,
                start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            self._base_revision = ingest_result.base_revision
            self._repository_url = ingest_result.repository_url
            task_spec_ref = ingest_result.task_spec_ref
            if ingest_result.requires_requirements_approval:
                approval = await self._wait_for_approval(
                    workflow_input,
                    kind="requirements",
                    status=RunStatus.WAITING_REQUIREMENTS_APPROVAL,
                )
                if approval.decision == "reject":
                    return await self._finalize(
                        workflow_input,
                        RunStatus.REJECTED,
                        error=self._error(ErrorCode.APPROVAL_REJECTED, "requirements rejected"),
                    )
                assert approval.replacement_acceptance_criteria is not None
                task_spec_ref = await workflow.execute_activity_method(
                    IngestActivities.finalize_task_spec,
                    FinalizeTaskSpecInput(
                        create_request_ref=workflow_input.create_request_ref,
                        base_revision=ingest_result.base_revision,
                        acceptance_criteria=approval.replacement_acceptance_criteria,
                    ),
                    task_queue=REPOSITORY_TASK_QUEUE,
                    start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                    retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
                )
            if task_spec_ref is None:
                raise RuntimeError("ingest_task 未返回可执行的 TaskSpec")
            snapshot_ref = await workflow.execute_activity_method(
                RepositoryActivities.scan_repository,
                task_spec_ref,
                task_queue=REPOSITORY_TASK_QUEUE,
                start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            dependency_result = await workflow.execute_activity_method(
                VerificationActivities.prepare_dependencies,
                PrepareDependenciesInput(
                    snapshot_ref=snapshot_ref,
                    task_spec_ref=task_spec_ref,
                ),
                task_queue=SANDBOX_TASK_QUEUE,
                start_to_close_timeout=_SANDBOX_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            await self._transition(workflow_input, RunStatus.BASELINING)
            baseline_report_ref = await workflow.execute_activity_method(
                VerificationActivities.verify_baseline,
                VerifyBaselineInput(
                    snapshot_ref=snapshot_ref,
                    dependency_layer_key=dependency_result.dependency_layer_key,
                ),
                task_queue=SANDBOX_TASK_QUEUE,
                start_to_close_timeout=_SANDBOX_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            await self._transition(workflow_input, RunStatus.PLANNING)
            planning_result = await workflow.execute_activity_method(
                PlanningActivities.plan_change,
                PlanChangeInput(
                    task_spec_ref=task_spec_ref,
                    repository_snapshot_ref=snapshot_ref,
                ),
                task_queue=MODEL_TASK_QUEUE,
                schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                start_to_close_timeout=_MODEL_START_TO_CLOSE,
                retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
            )
            approval_stages = self._resolve_approval_stages(
                workflow_input,
                planned_risk=planning_result.risk_level,
            )

            if approval_stages.plan is ApprovalMode.MANUAL:
                approval = await self._wait_for_approval(
                    workflow_input,
                    kind="plan",
                    status=RunStatus.WAITING_PLAN_APPROVAL,
                )
                if approval.decision == "reject":
                    return await self._finalize(
                        workflow_input,
                        RunStatus.REJECTED,
                        error=self._error(ErrorCode.APPROVAL_REJECTED, "plan rejected"),
                    )

            await self._transition(workflow_input, RunStatus.DESIGNING_TESTS)
            test_plan_ref = await workflow.execute_activity_method(
                QaActivities.design_sealed_tests,
                DesignSealedTestsInput(
                    task_spec_ref=task_spec_ref,
                    repository_snapshot_ref=snapshot_ref,
                ),
                task_queue=MODEL_TASK_QUEUE,
                schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                start_to_close_timeout=_MODEL_START_TO_CLOSE,
                retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
            )
            sealed_test_result = await workflow.execute_activity_method(
                VerificationActivities.verify_sealed_tests_on_base,
                VerifySealedTestsInput(
                    snapshot_ref=snapshot_ref,
                    test_plan_ref=test_plan_ref,
                    dependency_layer_key=dependency_result.dependency_layer_key,
                ),
                task_queue=SANDBOX_TASK_QUEUE,
                start_to_close_timeout=_SANDBOX_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            if not sealed_test_result.valid:
                approval = await self._wait_for_approval(
                    workflow_input,
                    kind="test",
                    status=RunStatus.WAITING_TEST_APPROVAL,
                )
                if approval.decision == "reject":
                    return await self._finalize(
                        workflow_input,
                        RunStatus.REJECTED,
                        error=self._error(ErrorCode.APPROVAL_REJECTED, "test plan rejected"),
                    )

            if approval_stages.execution is ApprovalMode.MANUAL:
                approval = await self._wait_for_approval(
                    workflow_input,
                    kind="execution",
                    status=RunStatus.WAITING_EXECUTION_APPROVAL,
                )
                if approval.decision == "reject":
                    return await self._finalize(
                        workflow_input,
                        RunStatus.REJECTED,
                        error=self._error(ErrorCode.APPROVAL_REJECTED, "execution rejected"),
                    )

            await self._transition(workflow_input, RunStatus.EXECUTING)
            work_items = {item.work_item_id: item for item in planning_result.work_items}
            planned_files: dict[UUID, list[PlannedFileChange]] = {}
            for planned_file in planning_result.planned_files:
                planned_files.setdefault(planned_file.work_item_id, []).append(planned_file)
            integrated_by_item: dict[UUID, ArtifactRef] = {}
            for wave in planning_result.waves:
                contexts = await asyncio.gather(
                    *(
                        workflow.execute_activity_method(
                            RepositoryActivities.build_developer_context,
                            BuildDeveloperContextInput(
                                work_item=work_items[item_id],
                                task_spec_ref=task_spec_ref,
                                integrated_patch_refs=tuple(
                                    integrated_by_item[dependency]
                                    for dependency in work_items[item_id].dependencies
                                ),
                            ),
                            task_queue=REPOSITORY_TASK_QUEUE,
                            start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                            retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
                        )
                        for item_id in wave
                    )
                )
                proposals = await asyncio.gather(
                    *(
                        workflow.execute_activity_method(
                            DeveloperActivities.develop_patch,
                            DevelopPatchInput(
                                developer_context_ref=context_ref,
                                task_spec_ref=task_spec_ref,
                                planned_files=tuple(planned_files[item_id]),
                            ),
                            task_queue=MODEL_TASK_QUEUE,
                            schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                            start_to_close_timeout=_MODEL_START_TO_CLOSE,
                            retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
                        )
                        for item_id, context_ref in zip(wave, contexts, strict=True)
                    )
                )
                for item_id, proposal_ref in zip(wave, proposals, strict=True):
                    integrated_by_item[item_id] = await workflow.execute_activity_method(
                        RepositoryActivities.integrate_patch,
                        IntegratePatchInput(
                            proposal_ref=proposal_ref,
                            work_item=work_items[item_id],
                            task_spec_ref=task_spec_ref,
                        ),
                        task_queue=REPOSITORY_TASK_QUEUE,
                        start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                        retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
                    )
            await self._transition(workflow_input, RunStatus.VERIFYING)
            candidate = await workflow.execute_activity_method(
                RepositoryActivities.export_candidate,
                task_spec_ref.run_id,
                task_queue=REPOSITORY_TASK_QUEUE,
                start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            self._final_revision = candidate.revision
            diff_ref = await workflow.execute_activity_method(
                RepositoryActivities.build_final_diff,
                task_spec_ref.run_id,
                task_queue=REPOSITORY_TASK_QUEUE,
                start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            self._patch_ref = diff_ref
            verification_result = await workflow.execute_activity_method(
                VerificationActivities.verify_candidate,
                VerifyCandidateInput(
                    snapshot_ref=snapshot_ref,
                    baseline_report_ref=baseline_report_ref,
                    test_plan_ref=test_plan_ref,
                    candidate_source_ref=candidate.source_archive_ref,
                    candidate_revision=candidate.revision,
                    dependency_layer_key=dependency_result.dependency_layer_key,
                ),
                task_queue=SANDBOX_TASK_QUEUE,
                start_to_close_timeout=_SANDBOX_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            self._verification_ref = verification_result.report_ref
            if not verification_result.passed:
                return await self._finalize(
                    workflow_input,
                    RunStatus.FAILED,
                    error=self._error(
                        ErrorCode.VERIFICATION_FAILED,
                        "candidate verification failed",
                        verification_result.report_ref,
                    ),
                )
            await self._transition(workflow_input, RunStatus.REVIEWING)
            review_result = await workflow.execute_activity_method(
                ReviewerActivities.review_candidate,
                ReviewCandidateInput(
                    task_spec_ref=task_spec_ref,
                    diff_ref=diff_ref,
                    verification_ref=verification_result.report_ref,
                ),
                task_queue=MODEL_TASK_QUEUE,
                schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                start_to_close_timeout=_MODEL_START_TO_CLOSE,
                retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
            )
            self._review_ref = review_result.review_ref
            if review_result.decision == "reject":
                return await self._finalize(
                    workflow_input,
                    RunStatus.REJECTED,
                    error=self._error(
                        ErrorCode.REVIEW_REJECTED,
                        "reviewer rejected candidate",
                        review_result.review_ref,
                    ),
                )
            if review_result.decision == "request_changes":
                return await self._finalize(
                    workflow_input,
                    RunStatus.FAILED,
                    error=self._error(
                        ErrorCode.REVIEW_REJECTED,
                        "reviewer requested changes and repair is exhausted",
                        review_result.review_ref,
                    ),
                )

            if approval_stages.delivery is ApprovalMode.AUTOMATIC:
                return await self._finalize(workflow_input, RunStatus.SUCCEEDED)

            approval = await self._wait_for_approval(
                workflow_input,
                kind="delivery",
                status=RunStatus.WAITING_DELIVERY_APPROVAL,
            )
            if approval.decision == "reject":
                return await self._finalize(
                    workflow_input,
                    RunStatus.REJECTED,
                    error=self._error(ErrorCode.APPROVAL_REJECTED, "delivery rejected"),
                )
            return await self._finalize(workflow_input, RunStatus.SUCCEEDED)
        except (asyncio.CancelledError, temporalio.exceptions.CancelledError):
            # cleanup cancellation scope 的最小版本：取消请求到达后，仍然
            # 执行一次 finalize，把状态机推进到 CANCELLED，而不是让 Workflow
            # Task 直接以异常结束、停留在一个非终态上。
            return await asyncio.shield(
                self._finalize(
                    workflow_input,
                    RunStatus.CANCELLED,
                    error=self._error(ErrorCode.WORKFLOW_CANCELLED, "workflow cancelled"),
                )
            )
        except Exception as exc:
            # Unexpected Activity failures must still pass through FINALIZING. Do not
            # expose untrusted exception text (which may contain credentials) in the
            # user-facing report; the activity history retains diagnostic detail.
            if self._status is RunStatus.FINALIZING:
                raise
            if workflow.cancellation_reason() is not None:
                return await asyncio.shield(
                    self._finalize(
                        workflow_input,
                        RunStatus.CANCELLED,
                        error=self._error(ErrorCode.WORKFLOW_CANCELLED, "workflow cancelled"),
                    )
                )
            return await self._finalize(
                workflow_input,
                RunStatus.FAILED,
                error=self._error(
                    ErrorCode.PLATFORM_ERROR,
                    f"workflow stage failed: {type(exc).__name__}",
                ),
            )

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
        self._approvals[request.kind] = request

    @staticmethod
    def _resolve_approval_stages(
        workflow_input: CodeRepairWorkflowInput,
        planned_risk: RiskLevel | None = None,
    ) -> ApprovalStagePolicy:
        risk_order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
        effective_risk = workflow_input.risk_level
        if planned_risk is not None and risk_order[planned_risk] > risk_order[effective_risk]:
            effective_risk = planned_risk
        stages = resolve_approval_policy(
            effective_risk,
            workflow_input.approval_policy,
        ).stages
        if workflow_input.auto_approve_low_risk is None:
            return stages
        return stages.model_copy(
            update={
                "delivery": (
                    ApprovalMode.AUTOMATIC
                    if workflow_input.auto_approve_low_risk
                    else ApprovalMode.MANUAL
                )
            }
        )

    async def _wait_for_approval(
        self,
        workflow_input: CodeRepairWorkflowInput,
        *,
        kind: ApprovalKind,
        status: RunStatus,
    ) -> ApprovalRequest:
        await self._transition(workflow_input, status)
        await workflow.wait_condition(lambda: kind in self._approvals)
        return self._approvals[kind]

    async def _transition(
        self, workflow_input: CodeRepairWorkflowInput, new_status: RunStatus
    ) -> None:
        validate_transition(self._status, new_status)
        self._status = new_status
        await self._record_projection(workflow_input)

    async def _finalize(
        self,
        workflow_input: CodeRepairWorkflowInput,
        outcome: RunStatus,
        *,
        error: ErrorInfo | None = None,
    ) -> CodeRepairWorkflowOutput:
        if self._status is not RunStatus.FINALIZING:
            await self._transition(workflow_input, RunStatus.FINALIZING)
        warnings: list[str] = []
        sandbox_cleanup_ref: ArtifactRef | None = None
        repository_cleanup_ref: ArtifactRef | None = None
        try:
            sandbox_cleanup_ref = await workflow.execute_activity_method(
                VerificationActivities.cleanup_sandboxes,
                workflow_input.run_id,
                task_queue=SANDBOX_TASK_QUEUE,
                start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                retry_policy=_CLEANUP_RETRY_POLICY,
            )
        except Exception as exc:
            warnings.append(f"sandbox cleanup failed: {type(exc).__name__}")
        try:
            repository_cleanup_ref = await workflow.execute_activity_method(
                RepositoryActivities.cleanup_repository,
                workflow_input.run_id,
                task_queue=REPOSITORY_TASK_QUEUE,
                start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                retry_policy=_CLEANUP_RETRY_POLICY,
            )
        except Exception as exc:
            warnings.append(f"repository cleanup failed: {type(exc).__name__}")

        if self._base_revision is None:
            warnings.append("ingest did not establish repository metadata")
        final_report_ref = await workflow.execute_activity_method(
            FinalizationActivities.build_final_report,
            BuildFinalReportInput(
                run_id=workflow_input.run_id,
                tenant_id=workflow_input.tenant_id,
                status=outcome,
                repository_url=self._repository_url,
                base_revision=self._base_revision or "0" * 40,
                final_revision=self._final_revision,
                patch_ref=self._patch_ref,
                verification_ref=self._verification_ref,
                review_ref=self._review_ref,
                repository_cleanup_ref=repository_cleanup_ref,
                sandbox_cleanup_ref=sandbox_cleanup_ref,
                warnings=tuple(warnings),
                started_at=self._started_at or workflow.now(),
                finished_at=workflow.now(),
                repair_rounds=self._repair_rounds,
                error=error,
            ),
            start_to_close_timeout=_PROJECTION_START_TO_CLOSE,
            schedule_to_start_timeout=_PROJECTION_SCHEDULE_TO_START,
            retry_policy=_PROJECTION_RETRY_POLICY,
        )
        await self._transition(workflow_input, outcome)
        return CodeRepairWorkflowOutput(
            run_id=workflow_input.run_id,
            status=outcome,
            final_report_ref=final_report_ref,
            failure=error,
        )

    @staticmethod
    def _error(code: ErrorCode, message: str, details_ref: ArtifactRef | None = None) -> ErrorInfo:
        return ErrorInfo(
            code=code,
            message=message,
            retryable=False,
            source="workflow",
            details_ref=details_ref,
        )

    async def _record_projection(self, workflow_input: CodeRepairWorkflowInput) -> None:
        event = ProjectionEvent(
            run_id=workflow_input.run_id,
            tenant_id=workflow_input.tenant_id,
            workflow_id=self._workflow_id,
            status=self._status,
            base_revision=self._base_revision,
            sequence=self._projection_sequence,
            occurred_at=workflow.now(),
        )
        await workflow.execute_activity_method(
            ProjectionActivities.update_projection,
            event,
            start_to_close_timeout=_PROJECTION_START_TO_CLOSE,
            schedule_to_start_timeout=_PROJECTION_SCHEDULE_TO_START,
            retry_policy=_PROJECTION_RETRY_POLICY,
        )
        self._projection_sequence += 1

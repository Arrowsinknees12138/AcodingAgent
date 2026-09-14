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
from temporalio.exceptions import ActivityError, ApplicationError

from repopilot.activities.blackboard import BlackboardActivities, PublishBlackboardInput
from repopilot.activities.developer import (
    DevelopAgentPatchInput,
    DeveloperActivities,
    DevelopPatchInput,
)
from repopilot.activities.finalization import BuildFinalReportInput, FinalizationActivities
from repopilot.activities.ingest import FinalizeTaskSpecInput, IngestActivities
from repopilot.activities.investigator import InvestigateInput, InvestigatorActivities
from repopilot.activities.planning import PlanChangeInput, PlanChangeResult, PlanningActivities
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
    dependency_manifest_paths,
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
_QA_MAX_REVISIONS = 2


class CodeRepairWorkflowInput(StrictModel):
    run_id: UUID
    tenant_id: UUID
    create_request_ref: ArtifactRef
    risk_level: RiskLevel = RiskLevel.LOW
    approval_policy: ApprovalPolicy = ApprovalPolicy()
    # Old workflow histories keep the one-shot Developer Activity during replay.
    developer_agent_mode: bool = False
    planner_agent_mode: bool = False
    investigator_agent_mode: bool = False
    shared_blackboard_enabled: bool = False
    reviewer_agent_mode: bool = False
    # Old histories keep the original repair allowlist; new runs may request
    # explicitly justified, risk-escalated scope expansion during replanning.
    scope_expansion_enabled: bool = False
    # Temporal 输入会保存在不可变 History 中；保留旧字段兼容已经启动的 Run。
    # 新调用方不应再传它。False 等价于额外要求 delivery 人工审批。
    auto_approve_low_risk: bool | None = None


class CodeRepairWorkflowOutput(StrictModel):
    run_id: UUID
    status: RunStatus
    final_report_ref: ArtifactRef | None
    failure: ErrorInfo | None


class CandidateAttemptResult(StrictModel):
    verification_passed: bool
    review_decision: str | None
    feedback_ref: ArtifactRef


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
            planner_agent_mode = workflow_input.planner_agent_mode and workflow.patched(
                "planner-agent-loop-v1"
            )
            planning_result = await workflow.execute_activity_method(
                (
                    PlanningActivities.plan_change_with_agent
                    if planner_agent_mode
                    else PlanningActivities.plan_change
                ),
                PlanChangeInput(
                    task_spec_ref=task_spec_ref,
                    repository_snapshot_ref=snapshot_ref,
                ),
                task_queue=MODEL_TASK_QUEUE,
                schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                start_to_close_timeout=_MODEL_START_TO_CLOSE,
                retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
            )
            blackboard_enabled = workflow_input.shared_blackboard_enabled and workflow.patched(
                "shared-blackboard-v1"
            )
            blackboard_ref: ArtifactRef | None = None
            if blackboard_enabled:
                blackboard_ref = await self._publish_blackboard(
                    planning_result.plan_ref, blackboard_ref
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
            # Old workflow histories must keep their original activity sequence on replay.
            qa_feedback_enabled = workflow.patched("qa-baseline-feedback-v1")
            previous_test_plan_ref: ArtifactRef | None = None
            sealed_baseline_report_ref: ArtifactRef | None = None
            max_qa_attempts = 1 + (_QA_MAX_REVISIONS if qa_feedback_enabled else 0)
            for qa_attempt in range(1, max_qa_attempts + 1):
                test_plan_ref = await workflow.execute_activity_method(
                    QaActivities.design_sealed_tests,
                    DesignSealedTestsInput(
                        task_spec_ref=task_spec_ref,
                        repository_snapshot_ref=snapshot_ref,
                        baseline_report_ref=baseline_report_ref if qa_feedback_enabled else None,
                        previous_test_plan_ref=previous_test_plan_ref,
                        sealed_baseline_report_ref=sealed_baseline_report_ref,
                        attempt=qa_attempt,
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
                if sealed_test_result.valid:
                    break
                if (
                    qa_feedback_enabled
                    and qa_attempt < max_qa_attempts
                    and sealed_test_result.runnable
                    and sealed_test_result.mismatch_count > 0
                ):
                    previous_test_plan_ref = test_plan_ref
                    sealed_baseline_report_ref = sealed_test_result.report_ref
                    continue
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
                break

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
            original_allowed_paths = tuple(file.path for file in planning_result.planned_files)
            highest_planned_risk = planning_result.risk_level
            repair_feedback_ref: ArtifactRef | None = None
            investigation_ref: ArtifactRef | None = None
            while True:
                attempt = await self._run_candidate_attempt(
                    workflow_input=workflow_input,
                    task_spec_ref=task_spec_ref,
                    snapshot_ref=snapshot_ref,
                    baseline_report_ref=baseline_report_ref,
                    test_plan_ref=test_plan_ref,
                    dependency_layer_key=dependency_result.dependency_layer_key,
                    plan=planning_result,
                    repair_feedback_ref=repair_feedback_ref,
                    investigation_ref=investigation_ref,
                    blackboard_ref=blackboard_ref,
                )
                if attempt.verification_passed and attempt.review_decision == "approve":
                    break
                if attempt.review_decision == "reject":
                    return await self._finalize(
                        workflow_input,
                        RunStatus.REJECTED,
                        error=self._error(
                            ErrorCode.REVIEW_REJECTED,
                            "reviewer rejected candidate",
                            attempt.feedback_ref,
                        ),
                    )
                if self._repair_rounds >= 2:
                    code = (
                        ErrorCode.VERIFICATION_FAILED
                        if not attempt.verification_passed
                        else ErrorCode.REVIEW_REJECTED
                    )
                    return await self._finalize(
                        workflow_input,
                        RunStatus.FAILED,
                        error=self._error(code, "repair rounds exhausted", attempt.feedback_ref),
                    )

                self._repair_rounds += 1
                repair_feedback_ref = attempt.feedback_ref
                if blackboard_enabled:
                    blackboard_ref = await self._publish_blackboard(
                        repair_feedback_ref, blackboard_ref
                    )
                await self._transition(workflow_input, RunStatus.REPLANNING)
                current_snapshot_ref = await workflow.execute_activity_method(
                    RepositoryActivities.scan_repository,
                    task_spec_ref,
                    task_queue=REPOSITORY_TASK_QUEUE,
                    start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                    retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
                )
                if workflow_input.investigator_agent_mode and workflow.patched(
                    "investigator-agent-v1"
                ):
                    investigation_ref = await workflow.execute_activity_method(
                        InvestigatorActivities.investigate_failure,
                        InvestigateInput(
                            task_spec_ref=task_spec_ref,
                            repository_snapshot_ref=current_snapshot_ref,
                            repair_feedback_ref=repair_feedback_ref,
                            attempt=self._repair_rounds + 1,
                            blackboard_ref=blackboard_ref,
                        ),
                        task_queue=MODEL_TASK_QUEUE,
                        schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                        start_to_close_timeout=_MODEL_START_TO_CLOSE,
                        retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
                    )
                    if blackboard_enabled:
                        blackboard_ref = await self._publish_blackboard(
                            investigation_ref, blackboard_ref
                        )
                await self._transition(workflow_input, RunStatus.PLANNING)
                planning_result = await workflow.execute_activity_method(
                    (
                        PlanningActivities.plan_change_with_agent
                        if planner_agent_mode
                        else PlanningActivities.plan_change
                    ),
                    PlanChangeInput(
                        task_spec_ref=task_spec_ref,
                        repository_snapshot_ref=current_snapshot_ref,
                        attempt=self._repair_rounds + 1,
                        repair_feedback_ref=repair_feedback_ref,
                        allowed_repair_paths=original_allowed_paths,
                        allow_scope_expansion=workflow_input.scope_expansion_enabled,
                        investigation_ref=investigation_ref,
                        blackboard_ref=blackboard_ref,
                    ),
                    task_queue=MODEL_TASK_QUEUE,
                    schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                    start_to_close_timeout=_MODEL_START_TO_CLOSE,
                    retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
                )
                if blackboard_enabled:
                    blackboard_ref = await self._publish_blackboard(
                        planning_result.plan_ref, blackboard_ref
                    )
                risk_order = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2}
                if risk_order[planning_result.risk_level] > risk_order[highest_planned_risk]:
                    highest_planned_risk = planning_result.risk_level
                repair_approvals = self._resolve_approval_stages(
                    workflow_input, planned_risk=highest_planned_risk
                )
                if repair_approvals.plan is ApprovalMode.MANUAL:
                    approval = await self._wait_for_approval(
                        workflow_input,
                        kind="plan",
                        status=RunStatus.WAITING_PLAN_APPROVAL,
                    )
                    if approval.decision == "reject":
                        return await self._finalize(
                            workflow_input,
                            RunStatus.REJECTED,
                            error=self._error(ErrorCode.APPROVAL_REJECTED, "repair plan rejected"),
                        )
                if repair_approvals.execution is ApprovalMode.MANUAL:
                    approval = await self._wait_for_approval(
                        workflow_input,
                        kind="execution",
                        status=RunStatus.WAITING_EXECUTION_APPROVAL,
                    )
                    if approval.decision == "reject":
                        return await self._finalize(
                            workflow_input,
                            RunStatus.REJECTED,
                            error=self._error(
                                ErrorCode.APPROVAL_REJECTED, "repair execution rejected"
                            ),
                        )
                if planning_result.scope_expansion_paths:
                    original_allowed_paths = tuple(
                        sorted(
                            set(original_allowed_paths).union(planning_result.scope_expansion_paths)
                        )
                    )
                await self._transition(workflow_input, RunStatus.EXECUTING)

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
            return await asyncio.shield(
                self._finalize(
                    workflow_input,
                    RunStatus.CANCELLED,
                    error=self._error(ErrorCode.WORKFLOW_CANCELLED, "workflow cancelled"),
                )
            )
        except Exception as exc:
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
                error=self._stage_error(exc),
            )

    @staticmethod
    def _stage_error(exc: Exception) -> ErrorInfo:
        # Only explicit, trusted Activity error types are surfaced. Arbitrary
        # exception messages may contain repository content or credentials.
        if isinstance(exc, ActivityError) and isinstance(exc.cause, ApplicationError):
            known = {
                "BUDGET_EXCEEDED": ErrorCode.BUDGET_EXCEEDED,
                "MODEL_COMPLETION_UNKNOWN": ErrorCode.MODEL_COMPLETION_UNKNOWN,
                "POLICY_DENIED": ErrorCode.POLICY_DENIED,
                "DEVELOPER_OUTPUT_INVALID": ErrorCode.MODEL_OUTPUT_INVALID,
            }
            code = known.get(exc.cause.type or "")
            if code is not None:
                return ErrorInfo(
                    code=code,
                    message=f"workflow stage failed: {code.value}",
                    retryable=False,
                    source="workflow",
                )
        return ErrorInfo(
            code=ErrorCode.PLATFORM_ERROR,
            message=f"workflow stage failed: {type(exc).__name__}",
            retryable=False,
            source="workflow",
        )

    @staticmethod
    async def _publish_blackboard(
        source_ref: ArtifactRef, previous_ref: ArtifactRef | None
    ) -> ArtifactRef:
        return await workflow.execute_activity_method(
            BlackboardActivities.publish_blackboard,
            PublishBlackboardInput(source_ref=source_ref, previous_ref=previous_ref),
            task_queue=MODEL_TASK_QUEUE,
            schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
            start_to_close_timeout=_MODEL_START_TO_CLOSE,
            retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
        )

    async def _run_candidate_attempt(
        self,
        *,
        workflow_input: CodeRepairWorkflowInput,
        task_spec_ref: ArtifactRef,
        snapshot_ref: ArtifactRef,
        baseline_report_ref: ArtifactRef,
        test_plan_ref: ArtifactRef,
        dependency_layer_key: str,
        plan: PlanChangeResult,
        repair_feedback_ref: ArtifactRef | None,
        investigation_ref: ArtifactRef | None,
        blackboard_ref: ArtifactRef | None,
    ) -> CandidateAttemptResult:
        agent_mode = workflow_input.developer_agent_mode and workflow.patched(
            "developer-agent-loop-v1"
        )
        work_items = {item.work_item_id: item for item in plan.work_items}
        planned_files: dict[UUID, list[PlannedFileChange]] = {}
        for planned_file in plan.planned_files:
            planned_files.setdefault(planned_file.work_item_id, []).append(planned_file)
        integrated_by_item: dict[UUID, ArtifactRef] = {}
        for wave in plan.waves:
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

            async def develop(item_id: UUID, context_ref: ArtifactRef) -> ArtifactRef:
                if agent_mode:
                    return await workflow.execute_activity_method(
                        DeveloperActivities.develop_patch_with_agent,
                        DevelopAgentPatchInput(
                            developer_context_ref=context_ref,
                            task_spec_ref=task_spec_ref,
                            planned_files=tuple(planned_files[item_id]),
                            repair_feedback_ref=repair_feedback_ref,
                            investigation_ref=investigation_ref,
                            blackboard_ref=blackboard_ref,
                            dependency_layer_key=dependency_layer_key,
                        ),
                        task_queue=MODEL_TASK_QUEUE,
                        schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                        start_to_close_timeout=_MODEL_START_TO_CLOSE,
                        retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
                    )
                return await workflow.execute_activity_method(
                    DeveloperActivities.develop_patch,
                    DevelopPatchInput(
                        developer_context_ref=context_ref,
                        task_spec_ref=task_spec_ref,
                        planned_files=tuple(planned_files[item_id]),
                        repair_feedback_ref=repair_feedback_ref,
                    ),
                    task_queue=MODEL_TASK_QUEUE,
                    schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
                    start_to_close_timeout=_MODEL_START_TO_CLOSE,
                    retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
                )

            proposals = await asyncio.gather(
                *(
                    develop(item_id, context_ref)
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
        candidate_dependency_key = dependency_layer_key
        if dependency_manifest_paths(tuple(file.path for file in plan.planned_files)):
            current_snapshot_ref = await workflow.execute_activity_method(
                RepositoryActivities.scan_repository,
                task_spec_ref,
                task_queue=REPOSITORY_TASK_QUEUE,
                start_to_close_timeout=_REPOSITORY_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            prepared = await workflow.execute_activity_method(
                VerificationActivities.prepare_dependencies,
                PrepareDependenciesInput(
                    snapshot_ref=current_snapshot_ref,
                    task_spec_ref=task_spec_ref,
                ),
                task_queue=SANDBOX_TASK_QUEUE,
                start_to_close_timeout=_SANDBOX_START_TO_CLOSE,
                retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
            )
            candidate_dependency_key = prepared.dependency_layer_key
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
                dependency_layer_key=candidate_dependency_key,
            ),
            task_queue=SANDBOX_TASK_QUEUE,
            start_to_close_timeout=_SANDBOX_START_TO_CLOSE,
            retry_policy=_SERVICE_ACTIVITY_RETRY_POLICY,
        )
        self._verification_ref = verification_result.report_ref
        if not verification_result.passed:
            return CandidateAttemptResult(
                verification_passed=False,
                review_decision=None,
                feedback_ref=verification_result.report_ref,
            )
        await self._transition(workflow_input, RunStatus.REVIEWING)
        review_result = await workflow.execute_activity_method(
            (
                ReviewerActivities.review_candidate_with_agent
                if workflow_input.reviewer_agent_mode and workflow.patched("reviewer-agent-loop-v1")
                else ReviewerActivities.review_candidate
            ),
            ReviewCandidateInput(
                task_spec_ref=task_spec_ref,
                diff_ref=diff_ref,
                verification_ref=verification_result.report_ref,
                attempt=self._repair_rounds + 1,
                candidate_source_ref=candidate.source_archive_ref,
                blackboard_ref=blackboard_ref,
            ),
            task_queue=MODEL_TASK_QUEUE,
            schedule_to_start_timeout=_MODEL_SCHEDULE_TO_START,
            start_to_close_timeout=_MODEL_START_TO_CLOSE,
            retry_policy=_MODEL_ACTIVITY_RETRY_POLICY,
        )
        self._review_ref = review_result.review_ref
        return CandidateAttemptResult(
            verification_passed=True,
            review_decision=review_result.decision,
            feedback_ref=review_result.review_ref,
        )

    @workflow.query
    def get_status(self) -> RunStatus:
        return self._status

    @workflow.query
    def get_repair_rounds(self) -> int:
        return self._repair_rounds

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
        return self._approvals.pop(kind)

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

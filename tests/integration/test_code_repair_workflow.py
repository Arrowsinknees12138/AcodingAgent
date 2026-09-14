"""`CodeRepairWorkflow` 集成测试：真实 Temporal 编排 + 真实 PostgreSQL 投影。

覆盖实施设计 Milestone 3 验收标准："Fake Activities 下 Run 可完成、
取消、恢复和查询"。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from temporalio.client import Client, WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from repopilot.activities.projections import ProjectionActivities
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import (
    ApprovalMode,
    ApprovalPolicyMode,
    ArtifactKind,
    RiskLevel,
    RunStatus,
)
from repopilot.domain.errors import ErrorCode
from repopilot.domain.policies import ApprovalPolicy, ApprovalStagePolicy
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.temporal.client import (
    ORCHESTRATION_TASK_QUEUE,
    start_code_repair_workflow,
    workflow_id_for,
)
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.services.task_queues import (
    MODEL_TASK_QUEUE,
    REPOSITORY_TASK_QUEUE,
    SANDBOX_TASK_QUEUE,
)
from repopilot.workflows.code_repair import CodeRepairWorkflow, CodeRepairWorkflowInput
from repopilot.workflows.updates import ApprovalRequest
from tests.integration.conftest import FakePipelineActivities

pytestmark = pytest.mark.integration


def _fake_create_request_ref(
    *,
    missing_acceptance_criteria: bool = False,
    candidate_should_fail: bool = False,
    reviewer_should_block: bool = False,
    ingest_should_fail: bool = False,
    candidate_repairs_once: bool = False,
    reviewer_repairs_once: bool = False,
    dependency_change: bool = False,
    sealed_tests_need_revision: bool = False,
    sealed_tests_never_match: bool = False,
    scope_expansion: bool = False,
) -> ArtifactRef:
    return ArtifactRef(
        artifact_id=uuid4(),
        run_id=uuid4(),
        tenant_id=uuid4(),
        kind=ArtifactKind.CREATE_RUN_REQUEST,
        schema_version="1",
        object_key="fake/key",
        sha256="a" * 64,
        size_bytes=(
            10
            if scope_expansion
            else 8
            if sealed_tests_need_revision
            else (
                9
                if sealed_tests_never_match
                else (
                    0
                    if missing_acceptance_criteria
                    else (
                        4
                        if ingest_should_fail
                        else (
                            5
                            if candidate_repairs_once
                            else (
                                7
                                if dependency_change
                                else (
                                    6
                                    if reviewer_repairs_once
                                    else (
                                        2
                                        if candidate_should_fail
                                        else (3 if reviewer_should_block else 1)
                                    )
                                )
                            )
                        )
                    )
                )
            )
        ),
        base_revision=None,
        input_artifact_ids=(),
        created_at=datetime.now(UTC),
    )


def _make_input(*, auto_approve_low_risk: bool) -> CodeRepairWorkflowInput:
    return CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(),
        auto_approve_low_risk=auto_approve_low_risk,
    )


def _make_policy_input(
    *,
    risk_level: RiskLevel,
    approval_policy: ApprovalPolicy | None = None,
) -> CodeRepairWorkflowInput:
    return CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(),
        risk_level=risk_level,
        approval_policy=approval_policy or ApprovalPolicy(),
    )


async def _wait_for_status(handle: object, expected: RunStatus) -> None:
    for _ in range(200):
        status = await handle.query(CodeRepairWorkflow.get_status)  # type: ignore[attr-defined]
        if status is expected:
            return
    pytest.fail(f"Run 没有在预期时间内进入 {expected.value}")


async def test_run_completes_when_auto_approved(temporal_client: Client) -> None:
    workflow_input = _make_input(auto_approve_low_risk=True)
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    result = await handle.result()
    assert result.status == RunStatus.SUCCEEDED
    assert result.final_report_ref is not None

    status = await handle.query(CodeRepairWorkflow.get_status)
    assert status == RunStatus.SUCCEEDED


async def test_new_run_dispatches_developer_agent_activity(
    temporal_client: Client, fake_pipeline: FakePipelineActivities
) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(),
        auto_approve_low_risk=True,
        developer_agent_mode=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED
    assert fake_pipeline.agent_development_calls[workflow_input.create_request_ref.run_id] == 1


async def test_qa_revises_mismatched_sealed_tests_before_execution(
    temporal_client: Client, fake_pipeline: FakePipelineActivities
) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(sealed_tests_need_revision=True),
        auto_approve_low_risk=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )

    assert (await handle.result()).status is RunStatus.SUCCEEDED
    assert fake_pipeline.qa_attempts[workflow_input.create_request_ref.run_id] == 2


async def test_qa_exhausts_revisions_then_requests_test_approval(
    temporal_client: Client, fake_pipeline: FakePipelineActivities
) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(sealed_tests_never_match=True),
        auto_approve_low_risk=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    await _wait_for_status(handle, RunStatus.WAITING_TEST_APPROVAL)
    assert fake_pipeline.qa_attempts[workflow_input.create_request_ref.run_id] == 3
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="test",
            decision="reject",
            actor_id="human-1",
            reason="baseline mismatch remains unresolved",
        ),
    )
    assert (await handle.result()).status is RunStatus.REJECTED


async def test_candidate_verification_failure_stops_before_review(
    temporal_client: Client,
) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(candidate_should_fail=True),
        auto_approve_low_risk=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )

    result = await handle.result()
    assert result.status is RunStatus.FAILED
    assert result.final_report_ref is not None
    assert result.failure is not None
    assert result.failure.code is ErrorCode.VERIFICATION_FAILED


async def test_ingest_failure_still_finalizes_without_exposing_exception_text(
    temporal_client: Client,
) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(ingest_should_fail=True),
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    result = await handle.result()
    assert result.status is RunStatus.FAILED
    assert result.final_report_ref is not None
    assert result.failure is not None
    assert result.failure.code is ErrorCode.PLATFORM_ERROR
    assert "secret" not in result.failure.message


@pytest.mark.parametrize("repair_source", ["verification", "reviewer"])
async def test_one_repair_round_can_recover(temporal_client: Client, repair_source: str) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(
            candidate_repairs_once=repair_source == "verification",
            reviewer_repairs_once=repair_source == "reviewer",
        ),
        auto_approve_low_risk=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    result = await handle.result()
    assert result.status is RunStatus.SUCCEEDED
    assert result.final_report_ref is not None


async def test_dependency_change_rebuilds_candidate_layer(temporal_client: Client) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(dependency_change=True),
        auto_approve_low_risk=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED


async def test_medium_risk_repair_requires_fresh_plan_approval(temporal_client: Client) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(candidate_repairs_once=True),
        risk_level=RiskLevel.MEDIUM,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    await _wait_for_status(handle, RunStatus.WAITING_PLAN_APPROVAL)
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="plan",
            decision="approve",
            actor_id="human-1",
            reason="first plan",
        ),
    )
    for _ in range(200):
        if await handle.query(CodeRepairWorkflow.get_repair_rounds) == 1:
            break
    else:
        pytest.fail("repair round did not begin")
    await _wait_for_status(handle, RunStatus.WAITING_PLAN_APPROVAL)
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="plan",
            decision="approve",
            actor_id="human-1",
            reason="repair plan",
        ),
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED


async def test_repair_scope_expansion_requires_plan_approval(temporal_client: Client) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(scope_expansion=True),
        scope_expansion_enabled=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    await _wait_for_status(handle, RunStatus.WAITING_PLAN_APPROVAL)
    assert await handle.query(CodeRepairWorkflow.get_repair_rounds) == 1
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="plan",
            decision="approve",
            actor_id="human-1",
            reason="approve justified helper path",
        ),
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED


async def test_reviewer_blocker_prevents_delivery(temporal_client: Client) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(reviewer_should_block=True),
        auto_approve_low_risk=True,
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )

    result = await handle.result()
    assert result.status is RunStatus.FAILED
    assert result.final_report_ref is not None
    assert result.failure is not None
    assert result.failure.code is ErrorCode.REVIEW_REJECTED


async def test_missing_acceptance_criteria_requires_requirements_approval(
    temporal_client: Client,
) -> None:
    workflow_input = CodeRepairWorkflowInput(
        run_id=uuid4(),
        tenant_id=uuid4(),
        create_request_ref=_fake_create_request_ref(missing_acceptance_criteria=True),
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    await _wait_for_status(handle, RunStatus.WAITING_REQUIREMENTS_APPROVAL)
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="requirements",
            decision="approve",
            actor_id="human-1",
            reason="criteria reviewed",
            replacement_acceptance_criteria=("approved criterion",),
        ),
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED


async def test_medium_risk_requires_plan_approval(temporal_client: Client) -> None:
    workflow_input = _make_policy_input(risk_level=RiskLevel.MEDIUM)
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    await _wait_for_status(handle, RunStatus.WAITING_PLAN_APPROVAL)
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="plan",
            decision="approve",
            actor_id="reviewer-1",
            reason="plan reviewed",
        ),
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED


async def test_high_risk_requires_plan_and_execution_approvals(
    temporal_client: Client,
) -> None:
    workflow_input = _make_policy_input(risk_level=RiskLevel.HIGH)
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    await _wait_for_status(handle, RunStatus.WAITING_PLAN_APPROVAL)
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="plan",
            decision="approve",
            actor_id="reviewer-1",
            reason="plan reviewed",
        ),
    )
    await _wait_for_status(handle, RunStatus.WAITING_EXECUTION_APPROVAL)
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="execution",
            decision="approve",
            actor_id="reviewer-1",
            reason="execution approved",
        ),
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED


async def test_custom_policy_can_require_only_delivery_approval(
    temporal_client: Client,
) -> None:
    workflow_input = _make_policy_input(
        risk_level=RiskLevel.HIGH,
        approval_policy=ApprovalPolicy(
            mode=ApprovalPolicyMode.CUSTOM,
            custom=ApprovalStagePolicy(
                plan=ApprovalMode.AUTOMATIC,
                execution=ApprovalMode.AUTOMATIC,
                delivery=ApprovalMode.MANUAL,
            ),
        ),
    )
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )
    await _wait_for_status(handle, RunStatus.WAITING_DELIVERY_APPROVAL)
    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="delivery",
            decision="approve",
            actor_id="reviewer-1",
            reason="delivery approved",
        ),
    )
    assert (await handle.result()).status is RunStatus.SUCCEEDED


async def test_run_can_be_queried_while_waiting_for_delivery_approval(
    temporal_client: Client,
) -> None:
    workflow_input = _make_input(auto_approve_low_risk=False)
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )

    async def _status() -> RunStatus:
        return await handle.query(CodeRepairWorkflow.get_status)  # type: ignore[no-any-return]

    for _ in range(200):
        if await _status() == RunStatus.WAITING_DELIVERY_APPROVAL:
            break
    else:
        pytest.fail("Run 没有在预期时间内进入 WAITING_DELIVERY_APPROVAL")

    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="delivery",
            decision="approve",
            actor_id="reviewer-1",
            reason="looks good",
        ),
    )
    result = await handle.result()
    assert result.status == RunStatus.SUCCEEDED


async def test_delivery_rejection_leads_to_rejected_status(temporal_client: Client) -> None:
    workflow_input = _make_input(auto_approve_low_risk=False)
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )

    async def _status() -> RunStatus:
        return await handle.query(CodeRepairWorkflow.get_status)  # type: ignore[no-any-return]

    for _ in range(200):
        if await _status() == RunStatus.WAITING_DELIVERY_APPROVAL:
            break
    else:
        pytest.fail("Run 没有在预期时间内进入 WAITING_DELIVERY_APPROVAL")

    await handle.execute_update(
        CodeRepairWorkflow.submit_approval,
        ApprovalRequest(
            approval_id=uuid4(),
            kind="delivery",
            decision="reject",
            actor_id="reviewer-1",
            reason="not ready",
        ),
    )
    result = await handle.result()
    assert result.status == RunStatus.REJECTED


async def test_duplicate_workflow_id_is_rejected(temporal_client: Client) -> None:
    # 用真正的生产启动路径（`start_code_repair_workflow`），而不是裸调用
    # `client.start_workflow`——Reject Duplicate 策略是在那个 helper 里设置
    # 的（第 8.1 节），不是 Temporal SDK 的默认行为；裸调用默认允许对同一个
    # Workflow ID 重复启动（只要上一个已经结束），测不出这条策略。
    workflow_input = _make_input(auto_approve_low_risk=True)
    handle = await start_code_repair_workflow(temporal_client, workflow_input)
    await handle.result()

    with pytest.raises(Exception):  # noqa: B017 - Temporal 抛的是内部 RPCError 子类
        await start_code_repair_workflow(temporal_client, workflow_input)


async def test_cancellation_leads_to_cancelled_status(temporal_client: Client) -> None:
    workflow_input = _make_input(auto_approve_low_risk=False)
    handle = await temporal_client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
    )

    async def _status() -> RunStatus:
        return await handle.query(CodeRepairWorkflow.get_status)  # type: ignore[no-any-return]

    for _ in range(200):
        if await _status() == RunStatus.WAITING_DELIVERY_APPROVAL:
            break
    else:
        pytest.fail("Run 没有在预期时间内进入 WAITING_DELIVERY_APPROVAL")

    await handle.cancel()
    try:
        result = await handle.result()
    except WorkflowFailureError:
        # 取决于 Temporal SDK 版本，Cancel 之后 result() 可能直接抛出
        # WorkflowFailureError，也可能正常返回 CANCELLED 的 Output——
        # 两种情况都必须落到 CANCELLED，用 describe() 兜底确认最终状态。
        description = await handle.describe()
        assert description.status.name == "CANCELED"
        return
    assert result.status == RunStatus.CANCELLED


async def test_worker_restart_recovers_pending_run(db_engine: object) -> None:
    """模拟 Worker 崩溃恢复：不复用共享的 `temporal_client` fixture（它的
    Worker 会一直跑到测试结束），而是自己起一个环境 + 第一个 Worker，把
    Run 推进到 WAITING_DELIVERY_APPROVAL 后**真正关掉**第一个 Worker，
    再起第二个 Worker 接管同一个 task queue，验证 Temporal 能在全新进程上
    继续处理这个 Run，而不是状态丢失或者只是"凑巧被同一个 Worker 处理"。

    两个 Worker 都显式 `max_cached_workflows=0`（关闭 sticky 缓存）：默认
    情况下 Workflow 执行会被"粘"在启动它的 Worker 的私有 sticky 队列上，
    第一个 Worker 退出后，Update 请求会一直等在那个没有任何 Worker 在轮询
    的 sticky 队列上——time-skipping 测试 Server 不会像真实 Server 那样
    自动把它超时回退到普通队列，所以不加这个选项这个测试会直接卡死。
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    projection_activities = ProjectionActivities(PostgresRunProjectionStore(session_factory))

    async with await WorkflowEnvironment.start_time_skipping(data_converter=data_converter) as env:
        fake_pipeline = FakePipelineActivities()
        repository_worker = Worker(
            env.client,
            task_queue=REPOSITORY_TASK_QUEUE,
            activities=[
                fake_pipeline.ingest_task,
                fake_pipeline.scan_repository,
                fake_pipeline.build_developer_context,
                fake_pipeline.integrate_patch,
                fake_pipeline.export_candidate,
                fake_pipeline.build_final_diff,
                fake_pipeline.cleanup_repository,
            ],
        )
        sandbox_worker = Worker(
            env.client,
            task_queue=SANDBOX_TASK_QUEUE,
            activities=[
                fake_pipeline.prepare_dependencies,
                fake_pipeline.verify_baseline,
                fake_pipeline.verify_sealed_tests_on_base,
                fake_pipeline.verify_candidate,
                fake_pipeline.cleanup_sandboxes,
            ],
        )
        model_worker = Worker(
            env.client,
            task_queue=MODEL_TASK_QUEUE,
            activities=[
                fake_pipeline.plan_change,
                fake_pipeline.design_sealed_tests,
                fake_pipeline.develop_patch,
                fake_pipeline.review_candidate,
            ],
        )
        workflow_input = _make_input(auto_approve_low_risk=False)
        first_worker = Worker(
            env.client,
            task_queue=ORCHESTRATION_TASK_QUEUE,
            workflows=[CodeRepairWorkflow],
            activities=[
                projection_activities.update_projection,
                fake_pipeline.build_final_report,
            ],
            max_cached_workflows=0,
        )
        async with repository_worker, sandbox_worker, model_worker:
            async with first_worker:
                handle = await env.client.start_workflow(
                    CodeRepairWorkflow.run,
                    workflow_input,
                    id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
                    task_queue=ORCHESTRATION_TASK_QUEUE,
                )

                async def _status() -> RunStatus:
                    return await handle.query(  # type: ignore[no-any-return]
                        CodeRepairWorkflow.get_status
                    )

                for _ in range(200):
                    if await _status() == RunStatus.WAITING_DELIVERY_APPROVAL:
                        break
                else:
                    pytest.fail("Run 没有在预期时间内进入 WAITING_DELIVERY_APPROVAL")
            # `async with first_worker` 退出 = 第一个 Worker 已经彻底停止轮询。

            second_worker = Worker(
                env.client,
                task_queue=ORCHESTRATION_TASK_QUEUE,
                workflows=[CodeRepairWorkflow],
                activities=[
                    projection_activities.update_projection,
                    fake_pipeline.build_final_report,
                ],
                max_cached_workflows=0,
            )
            async with second_worker:
                await handle.execute_update(
                    CodeRepairWorkflow.submit_approval,
                    ApprovalRequest(
                        approval_id=uuid4(),
                        kind="delivery",
                        decision="approve",
                        actor_id="reviewer-1",
                        reason="looks good",
                    ),
                )
                result = await handle.result()

    assert result.status == RunStatus.SUCCEEDED

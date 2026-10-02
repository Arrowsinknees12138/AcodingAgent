"""Temporal deadline and cancellation must still produce a terminal report."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from temporalio import activity
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from repopilot.activities.ci import CollectCiResult
from repopilot.activities.finalization import BuildFinalReportInput
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import ArtifactKind, RunStatus
from repopilot.domain.errors import ErrorCode
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.services.run_projection import ProjectionEvent
from repopilot.services.task_queues import (
    ORCHESTRATION_TASK_QUEUE,
    REPOSITORY_TASK_QUEUE,
    SANDBOX_TASK_QUEUE,
)
from repopilot.workflows.code_repair import CodeRepairWorkflow, CodeRepairWorkflowInput


class Activities:
    def __init__(self, ref):
        self.ref = ref
        self.final = None
        self.started = asyncio.Event()
        self.cleaned = []

    @activity.defn(name="update_projection")
    async def projection(self, payload: ProjectionEvent) -> None:
        pass

    @activity.defn(name="collect_ci_failure")
    async def collect(self, ref: ArtifactRef) -> CollectCiResult:
        self.started.set()
        await asyncio.sleep(3600)
        return CollectCiResult(request_ref=ref, ready=False)

    @activity.defn(name="cleanup_repository")
    async def repository(self, run_id: object) -> ArtifactRef:
        self.cleaned.append("repository")
        return self.ref.model_copy(
            update={"kind": ArtifactKind.CLEANUP_REPORT, "base_revision": "a" * 40}
        )

    @activity.defn(name="cleanup_sandboxes")
    async def sandbox(self, run_id: object) -> ArtifactRef:
        self.cleaned.append("sandbox")
        return self.ref.model_copy(
            update={"kind": ArtifactKind.CLEANUP_REPORT, "base_revision": "a" * 40}
        )

    @activity.defn(name="build_final_report")
    async def finalize(self, payload: BuildFinalReportInput) -> ArtifactRef:
        self.final = payload
        return self.ref.model_copy(
            update={"kind": ArtifactKind.FINAL_REPORT, "base_revision": "a" * 40}
        )


@pytest.mark.parametrize("cancel", [False, True])
async def test_ci_deadline_or_cancel_cleans_up_and_reports(cancel):
    ref = ArtifactRef(
        artifact_id=uuid4(),
        run_id=uuid4(),
        tenant_id=uuid4(),
        kind=ArtifactKind.CREATE_RUN_REQUEST,
        schema_version="1",
        object_key="fake",
        sha256="a" * 64,
        size_bytes=0,
        base_revision=None,
        input_artifact_ids=(),
        created_at=datetime.now(UTC),
    )
    impl = Activities(ref)
    async with await WorkflowEnvironment.start_time_skipping(data_converter=data_converter) as env:
        workers = (
            Worker(
                env.client,
                task_queue=ORCHESTRATION_TASK_QUEUE,
                workflows=[CodeRepairWorkflow],
                activities=[impl.projection, impl.finalize],
            ),
            Worker(
                env.client,
                task_queue=REPOSITORY_TASK_QUEUE,
                activities=[impl.collect, impl.repository],
            ),
            Worker(env.client, task_queue=SANDBOX_TASK_QUEUE, activities=[impl.sandbox]),
        )
        async with workers[0], workers[1], workers[2]:
            handle = await env.client.start_workflow(
                CodeRepairWorkflow.run,
                CodeRepairWorkflowInput(
                    run_id=ref.run_id,
                    tenant_id=ref.tenant_id,
                    create_request_ref=ref,
                    ci_import_enabled=True,
                    wall_timeout_seconds=3,
                ),
                id=f"deadline-{ref.run_id}",
                task_queue=ORCHESTRATION_TASK_QUEUE,
            )
            await asyncio.wait_for(impl.started.wait(), timeout=10)
            if cancel:
                await handle.cancel()
            result = await asyncio.wait_for(handle.result(), timeout=20)
    assert result.status is (RunStatus.CANCELLED if cancel else RunStatus.FAILED)
    assert result.failure.code is (
        ErrorCode.WORKFLOW_CANCELLED if cancel else ErrorCode.BUDGET_EXCEEDED
    )
    assert impl.final is not None and set(impl.cleaned) == {"repository", "sandbox"}

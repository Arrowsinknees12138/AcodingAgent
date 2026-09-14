"""Read-only, evidence-backed root cause investigation before repair."""

from __future__ import annotations

import json
from decimal import Decimal

from temporalio import activity
from temporalio.exceptions import ApplicationError

from repopilot.agents.loop import AgentLoop
from repopilot.domain import StrictModel
from repopilot.domain.agents import AgentExecutionRequest
from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    RepositorySnapshot,
)
from repopilot.domain.enums import AgentRole, ArtifactKind
from repopilot.domain.investigation import InvestigationReport
from repopilot.domain.tasks import TaskSpec
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.model_gateway import BudgetedModelGateway
from repopilot.services.repair_feedback import summarize_repair_feedback
from repopilot.services.scheduler import normalize_plan_path
from repopilot.tools.archive_workspace import ArchiveWorkspaceBackend
from repopilot.tools.finish import FinishTool
from repopilot.tools.read_file import ReadFileTool
from repopilot.tools.registry import ToolContext, ToolRegistry
from repopilot.tools.search_code import SearchCodeTool
from repopilot.tools.submit_investigation import SubmitInvestigationTool
from repopilot.tools.workspace_models import ReadFileRequest


class InvestigateInput(StrictModel):
    task_spec_ref: ArtifactRef
    repository_snapshot_ref: ArtifactRef
    repair_feedback_ref: ArtifactRef
    attempt: int


class InvestigatorActivities:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        gateway: BudgetedModelGateway,
        model: str,
        reservation_usd: Decimal,
    ) -> None:
        self._artifacts = artifact_store
        self._gateway = gateway
        self._model = model
        self._reservation_usd = reservation_usd

    @activity.defn(name="investigate_failure")
    async def investigate_failure(self, payload: InvestigateInput) -> ArtifactRef:
        refs = (
            payload.task_spec_ref,
            payload.repository_snapshot_ref,
            payload.repair_feedback_ref,
        )
        first = refs[0]
        if payload.attempt < 2 or any(
            ref.run_id != first.run_id or ref.tenant_id != first.tenant_id for ref in refs
        ):
            raise ApplicationError("Investigator input scope mismatch", non_retryable=True)
        if payload.repair_feedback_ref.kind not in {
            ArtifactKind.VERIFICATION_REPORT,
            ArtifactKind.REVIEW_DECISION,
        }:
            raise ApplicationError("Investigator feedback kind invalid", non_retryable=True)
        caller = ArtifactCaller(
            tenant_id=first.tenant_id,
            run_id=first.run_id,
            role=None,
            service="investigator-context-builder",
        )
        task = TaskSpec.model_validate_json(await self._artifacts.get_bytes(first, caller))
        snapshot = RepositorySnapshot.model_validate_json(
            await self._artifacts.get_bytes(payload.repository_snapshot_ref, caller)
        )
        feedback = summarize_repair_feedback(
            payload.repair_feedback_ref.kind,
            await self._artifacts.get_bytes(payload.repair_feedback_ref, caller),
        )
        source = await self._artifacts.get_bytes(snapshot.source_archive_ref, caller)
        backend = ArchiveWorkspaceBackend(
            archive=source,
            run_id=first.run_id,
            tenant_id=first.tenant_id,
            base_revision=snapshot.base_revision,
            artifact_store=self._artifacts,
            sandbox=None,
            dependency_layer_key=None,
        )
        metadata = ArtifactMetadata(
            tenant_id=first.tenant_id,
            run_id=first.run_id,
            base_revision=snapshot.base_revision,
            schema_version="1",
            input_artifact_ids=tuple(ref.artifact_id for ref in refs),
        )
        seed_ref = await self._artifacts.put_bytes(
            ArtifactKind.TRAJECTORY,
            json.dumps(
                {
                    "task": task.model_dump(mode="json"),
                    "safe_failure_summary": feedback,
                    "repository_paths": backend.paths,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode(),
            metadata,
        )
        submit = SubmitInvestigationTool()
        loop = AgentLoop(
            gateway=self._gateway,
            artifact_store=self._artifacts,
            tools=ToolRegistry(
                (SearchCodeTool(backend), ReadFileTool(backend), submit, FinishTool())
            ),
            model=self._model,
            reservation_usd=self._reservation_usd,
        )
        context = ToolContext(
            tenant_id=first.tenant_id,
            run_id=first.run_id,
            work_item_id=task.task_id,
            sandbox_id=None,
            read_paths=(),
            write_paths=(),
            read_all_repository_files=True,
        )
        result = await loop.execute(
            AgentExecutionRequest(
                role=AgentRole.INVESTIGATOR,
                work_item_id=task.task_id,
                input_refs=(seed_ref,),
                allowed_tools=("search_code", "read_file", "submit_investigation", "finish"),
                max_steps=8,
                remaining_model_calls=8,
                attempt=payload.attempt,
            ),
            context,
        )
        if result.status != "succeeded" or submit.report is None:
            message = result.error.message if result.error else "Investigator did not submit"
            raise ApplicationError(message, type="INVESTIGATION_INVALID", non_retryable=True)
        await self._validate_report(submit.report, backend, context)
        return await self._artifacts.put_bytes(
            ArtifactKind.INVESTIGATION,
            submit.report.model_dump_json().encode(),
            metadata.model_copy(
                update={
                    "input_artifact_ids": (
                        *metadata.input_artifact_ids,
                        *((result.trajectory_ref.artifact_id,) if result.trajectory_ref else ()),
                    )
                }
            ),
        )

    @staticmethod
    async def _validate_report(
        report: InvestigationReport,
        backend: ArchiveWorkspaceBackend,
        context: ToolContext,
    ) -> None:
        for evidence in report.evidence:
            path = normalize_plan_path(evidence.path)
            if path not in backend.paths:
                raise ApplicationError("Investigator cited a missing file", non_retryable=True)
            if evidence.line > 1_000_000:
                raise ApplicationError("Investigator line number invalid", non_retryable=True)
            observed = await backend.read_file(
                ReadFileRequest(
                    path=path,
                    start_line=evidence.line,
                    end_line=evidence.line,
                ),
                context,
            )
            if not observed.content:
                raise ApplicationError("Investigator cited a missing line", non_retryable=True)
        for path in report.suggested_paths:
            normalize_plan_path(path)

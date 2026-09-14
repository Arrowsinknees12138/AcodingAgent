"""Publish safe, auditable cross-agent summaries to an immutable blackboard."""

from __future__ import annotations

import json

from temporalio import activity
from temporalio.exceptions import ApplicationError

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.blackboard import BlackboardEntry, BlackboardState
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.investigation import InvestigationReport
from repopilot.domain.plans import ChangePlan
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.repair_feedback import summarize_repair_feedback


class PublishBlackboardInput(StrictModel):
    source_ref: ArtifactRef
    previous_ref: ArtifactRef | None = None


class BlackboardActivities:
    def __init__(self, *, artifact_store: ArtifactStore) -> None:
        self._artifacts = artifact_store

    @activity.defn(name="publish_blackboard")
    async def publish_blackboard(self, payload: PublishBlackboardInput) -> ArtifactRef:
        source_ref = payload.source_ref
        if source_ref.kind not in {
            ArtifactKind.CHANGE_PLAN,
            ArtifactKind.INVESTIGATION,
            ArtifactKind.VERIFICATION_REPORT,
            ArtifactKind.REVIEW_DECISION,
        }:
            raise ApplicationError("Blackboard source kind is not allowed", non_retryable=True)
        previous_ref = payload.previous_ref
        if previous_ref is not None and (
            previous_ref.kind is not ArtifactKind.BLACKBOARD
            or previous_ref.tenant_id != source_ref.tenant_id
            or previous_ref.run_id != source_ref.run_id
        ):
            raise ApplicationError("Blackboard scope mismatch", non_retryable=True)
        caller = ArtifactCaller(
            tenant_id=source_ref.tenant_id,
            run_id=source_ref.run_id,
            role=None,
            service="blackboard-publisher",
        )
        previous = (
            BlackboardState.model_validate_json(
                await self._artifacts.get_bytes(previous_ref, caller)
            )
            if previous_ref is not None
            else BlackboardState(entries=())
        )
        if len(previous.entries) >= 20:
            raise ApplicationError("Blackboard entry limit reached", non_retryable=True)
        content = await self._artifacts.get_bytes(source_ref, caller)
        entry = _safe_entry(source_ref, content)
        if any(item.source_artifact_id == source_ref.artifact_id for item in previous.entries):
            raise ApplicationError("Blackboard source already published", non_retryable=True)
        state = BlackboardState(entries=(*previous.entries, entry))
        return await self._artifacts.put_bytes(
            ArtifactKind.BLACKBOARD,
            state.model_dump_json().encode(),
            ArtifactMetadata(
                tenant_id=source_ref.tenant_id,
                run_id=source_ref.run_id,
                base_revision=source_ref.base_revision,
                schema_version="1",
                input_artifact_ids=(
                    *((previous_ref.artifact_id,) if previous_ref else ()),
                    source_ref.artifact_id,
                ),
            ),
        )


def _safe_entry(ref: ArtifactRef, content: bytes) -> BlackboardEntry:
    summary: dict[str, object]
    if ref.kind is ArtifactKind.CHANGE_PLAN:
        plan = ChangePlan.model_validate_json(content)
        author = "planner"
        summary = {
            "paths": [file.path for file in plan.files],
            "responsibilities": [file.responsibility[:300] for file in plan.files],
            "risk_flags": [flag.value for flag in plan.risk_flags],
            "scope_expansion_reason": plan.scope_expansion_reason,
        }
    elif ref.kind is ArtifactKind.INVESTIGATION:
        report = InvestigationReport.model_validate_json(content)
        author = "investigator"
        summary = {
            "root_cause": report.root_cause,
            "evidence": [item.model_dump(mode="json") for item in report.evidence],
            "suggested_paths": report.suggested_paths,
            "recommendation": report.recommendation,
            "confidence": report.confidence,
        }
    elif ref.kind in {ArtifactKind.VERIFICATION_REPORT, ArtifactKind.REVIEW_DECISION}:
        author = "verification" if ref.kind is ArtifactKind.VERIFICATION_REPORT else "reviewer"
        summary = summarize_repair_feedback(ref.kind, content)
    else:
        raise ValueError("unsupported blackboard source")
    return BlackboardEntry(
        author=author,
        source_kind=ref.kind,
        source_artifact_id=ref.artifact_id,
        summary=json.dumps(summary, ensure_ascii=False, sort_keys=True)[:8_000],
    )

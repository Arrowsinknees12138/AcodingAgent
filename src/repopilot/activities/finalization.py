"""Deterministic final report assembly at the trusted orchestration boundary."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from temporalio import activity

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import ArtifactKind, RunStatus
from repopilot.domain.errors import ErrorInfo
from repopilot.domain.tasks import CleanupReport, FinalReport
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.model_gateway import ModelUsageReader, ModelUsageTotals


class BuildFinalReportInput(StrictModel):
    run_id: UUID
    tenant_id: UUID
    status: RunStatus
    repository_url: str
    base_revision: str
    final_revision: str | None = None
    patch_ref: ArtifactRef | None = None
    verification_ref: ArtifactRef | None = None
    review_ref: ArtifactRef | None = None
    repository_cleanup_ref: ArtifactRef | None = None
    sandbox_cleanup_ref: ArtifactRef | None = None
    warnings: tuple[str, ...] = ()
    started_at: datetime
    finished_at: datetime
    sandbox_seconds: int = 0
    repair_rounds: int = 0
    error: ErrorInfo | None = None


class FinalizationActivities:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        usage_reader: ModelUsageReader | None = None,
    ) -> None:
        self._artifacts = artifact_store
        self._usage_reader = usage_reader

    @activity.defn(name="build_final_report")
    async def build_final_report(self, payload: BuildFinalReportInput) -> ArtifactRef:
        scoped_refs = tuple(
            ref
            for ref in (
                payload.patch_ref,
                payload.verification_ref,
                payload.review_ref,
                payload.repository_cleanup_ref,
                payload.sandbox_cleanup_ref,
            )
            if ref is not None
        )
        if any(
            ref.run_id != payload.run_id or ref.tenant_id != payload.tenant_id
            for ref in scoped_refs
        ):
            raise ValueError("final report input Artifact scope mismatch")

        cleanup = CleanupReport(
            run_id=payload.run_id,
            repository_cleanup_ref=payload.repository_cleanup_ref,
            sandbox_cleanup_ref=payload.sandbox_cleanup_ref,
            warnings=payload.warnings,
        )
        cleanup_ref = await self._artifacts.put_bytes(
            ArtifactKind.CLEANUP_REPORT,
            cleanup.model_dump_json().encode(),
            ArtifactMetadata(
                tenant_id=payload.tenant_id,
                run_id=payload.run_id,
                base_revision=payload.base_revision,
                schema_version="1",
                input_artifact_ids=tuple(
                    ref.artifact_id
                    for ref in (
                        payload.repository_cleanup_ref,
                        payload.sandbox_cleanup_ref,
                    )
                    if ref is not None
                ),
            ),
        )
        changed_paths = await self._changed_paths(payload)
        usage = await self._usage(payload)
        warnings = list(payload.warnings)
        if payload.sandbox_seconds == 0:
            warnings.append("sandbox execution time is not yet measured across activities")
        if usage.unknown_calls:
            warnings.append(
                f"{usage.unknown_calls} model call(s) have unknown completion or billing state"
            )
        report = FinalReport(
            run_id=payload.run_id,
            status=payload.status,
            repository_url=payload.repository_url,
            base_revision=payload.base_revision,
            final_revision=payload.final_revision,
            changed_paths=changed_paths,
            patch_ref=payload.patch_ref,
            verification_ref=payload.verification_ref,
            review_ref=payload.review_ref,
            cleanup_report_ref=cleanup_ref,
            warnings=tuple(warnings),
            model_calls=usage.model_calls,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            model_cost_usd=usage.cost_usd,
            sandbox_seconds=payload.sandbox_seconds,
            duration_seconds=max(
                0, int((payload.finished_at - payload.started_at).total_seconds())
            ),
            repair_rounds=payload.repair_rounds,
            error=payload.error,
        )
        return await self._artifacts.put_bytes(
            ArtifactKind.FINAL_REPORT,
            report.model_dump_json().encode(),
            ArtifactMetadata(
                tenant_id=payload.tenant_id,
                run_id=payload.run_id,
                base_revision=payload.base_revision,
                schema_version="1",
                input_artifact_ids=tuple(ref.artifact_id for ref in (*scoped_refs, cleanup_ref)),
            ),
        )

    async def _changed_paths(self, payload: BuildFinalReportInput) -> tuple[str, ...]:
        if payload.patch_ref is None:
            return ()
        caller = ArtifactCaller(
            tenant_id=payload.tenant_id,
            run_id=payload.run_id,
            role=None,
            service="finalization",
        )
        diff = (await self._artifacts.get_bytes(payload.patch_ref, caller)).decode(
            "utf-8", errors="replace"
        )
        paths: list[str] = []
        for line in diff.splitlines():
            if not (line.startswith("--- a/") or line.startswith("+++ b/")):
                continue
            path = line[6:]
            if path and path not in paths:
                paths.append(path)
        return tuple(paths)

    async def _usage(self, payload: BuildFinalReportInput) -> ModelUsageTotals:
        if self._usage_reader is None:
            return ModelUsageTotals(
                model_calls=0,
                input_tokens=0,
                output_tokens=0,
                cost_usd=Decimal("0"),
                unknown_calls=0,
            )
        return await self._usage_reader.get_run_usage(
            run_id=payload.run_id,
            tenant_id=payload.tenant_id,
        )

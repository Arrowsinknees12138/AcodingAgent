"""Final report assembly keeps cleanup, diff, usage, and error evidence together."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from repopilot.activities.finalization import BuildFinalReportInput, FinalizationActivities
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata
from repopilot.domain.enums import ArtifactKind, RunStatus
from repopilot.domain.tasks import CleanupReport, FinalReport
from repopilot.services.model_gateway import ModelUsageTotals
from tests.fakes import MemoryArtifactStore


class FakeUsageReader:
    async def get_run_usage(self, *, run_id: object, tenant_id: object) -> ModelUsageTotals:
        del run_id, tenant_id
        return ModelUsageTotals(
            model_calls=4,
            input_tokens=120,
            output_tokens=30,
            cost_usd=Decimal("0.0042"),
            unknown_calls=1,
        )


async def test_build_final_report_collects_cleanup_diff_and_usage() -> None:
    store = MemoryArtifactStore()
    run_id = uuid4()
    tenant_id = uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    patch_ref = await store.put_bytes(
        ArtifactKind.PATCH,
        b"--- a/src/old.py\n+++ b/src/new.py\n@@ -1 +1 @@\n-old\n+new\n",
        metadata,
    )
    repository_cleanup_ref = await store.put_bytes(
        ArtifactKind.CLEANUP_REPORT, b"repository", metadata
    )
    sandbox_cleanup_ref = await store.put_bytes(ArtifactKind.CLEANUP_REPORT, b"sandbox", metadata)
    started_at = datetime.now(UTC)
    activity_impl = FinalizationActivities(
        artifact_store=store,
        usage_reader=FakeUsageReader(),
    )

    final_ref = await activity_impl.build_final_report(
        BuildFinalReportInput(
            run_id=run_id,
            tenant_id=tenant_id,
            status=RunStatus.SUCCEEDED,
            repository_url="https://github.com/owner/repo",
            base_revision="a" * 40,
            final_revision="b" * 40,
            patch_ref=patch_ref,
            repository_cleanup_ref=repository_cleanup_ref,
            sandbox_cleanup_ref=sandbox_cleanup_ref,
            started_at=started_at,
            finished_at=started_at + timedelta(seconds=9),
        )
    )

    caller = ArtifactCaller(
        tenant_id=tenant_id,
        run_id=run_id,
        role=None,
        service="test",
    )
    report = FinalReport.model_validate_json(await store.get_bytes(final_ref, caller))
    assert report.changed_paths == ("src/old.py", "src/new.py")
    assert report.model_calls == 4
    assert report.model_cost_usd == Decimal("0.0042")
    assert report.duration_seconds == 9
    assert report.cleanup_report_ref is not None
    assert report.warnings == (
        "sandbox execution time is not yet measured across activities",
        "1 model call(s) have unknown completion or billing state",
    )

    cleanup = CleanupReport.model_validate_json(
        await store.get_bytes(report.cleanup_report_ref, caller)
    )
    assert cleanup.repository_cleanup_ref == repository_cleanup_ref
    assert cleanup.sandbox_cleanup_ref == sandbox_cleanup_ref

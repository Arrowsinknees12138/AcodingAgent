"""Verification Service 的 Temporal Activity 适配层。"""

from __future__ import annotations

from temporalio import activity

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactCaller, ArtifactRef
from repopilot.domain.verification import SealedTestBaselineReport
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.verification_service import (
    BaselineVerificationService,
    SealedTestBaselineService,
)


class VerifySealedTestsInput(StrictModel):
    snapshot_ref: ArtifactRef
    test_plan_ref: ArtifactRef


class VerifySealedTestsResult(StrictModel):
    report_ref: ArtifactRef
    valid: bool


class VerificationActivities:
    def __init__(
        self,
        baseline: BaselineVerificationService,
        *,
        sealed: SealedTestBaselineService | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._baseline = baseline
        self._sealed = sealed
        self._artifacts = artifact_store

    @activity.defn(name="verify_baseline")
    async def verify_baseline(self, snapshot_ref: ArtifactRef) -> ArtifactRef:
        return await self._baseline.verify(snapshot_ref)

    @activity.defn(name="verify_sealed_tests_on_base")
    async def verify_sealed_tests_on_base(
        self, payload: VerifySealedTestsInput
    ) -> VerifySealedTestsResult:
        if self._sealed is None or self._artifacts is None:
            raise RuntimeError("sealed test verifier 未配置")
        report_ref = await self._sealed.verify(
            snapshot_ref=payload.snapshot_ref,
            test_plan_ref=payload.test_plan_ref,
        )
        caller = ArtifactCaller(
            tenant_id=report_ref.tenant_id,
            run_id=report_ref.run_id,
            role=None,
            service="verification",
        )
        report = SealedTestBaselineReport.model_validate_json(
            await self._artifacts.get_bytes(report_ref, caller)
        )
        return VerifySealedTestsResult(report_ref=report_ref, valid=report.valid)

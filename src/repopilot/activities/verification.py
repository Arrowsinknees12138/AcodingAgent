"""Verification Service 的 Temporal Activity 适配层。"""

from __future__ import annotations

from uuid import UUID

from temporalio import activity

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    RepositorySnapshot,
)
from repopilot.domain.ci import CiDiagnosisReport
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import TaskSpec
from repopilot.domain.verification import (
    SealedTestBaselineReport,
    TestPlan,
    VerificationReport,
)
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.ci_diagnosis import CiDiagnosisService
from repopilot.services.sandbox_service import SandboxService
from repopilot.services.verification_service import (
    BaselineVerificationService,
    CandidateVerificationService,
    SealedTestBaselineService,
)


class VerifySealedTestsInput(StrictModel):
    snapshot_ref: ArtifactRef
    test_plan_ref: ArtifactRef
    dependency_layer_key: str


class VerifySealedTestsResult(StrictModel):
    report_ref: ArtifactRef
    valid: bool
    runnable: bool = True
    mismatch_count: int = 0


class VerifyCandidateInput(StrictModel):
    snapshot_ref: ArtifactRef
    baseline_report_ref: ArtifactRef
    test_plan_ref: ArtifactRef
    candidate_source_ref: ArtifactRef
    candidate_revision: str
    dependency_layer_key: str


class VerifyCandidateResult(StrictModel):
    report_ref: ArtifactRef
    passed: bool


class PrepareDependenciesInput(StrictModel):
    snapshot_ref: ArtifactRef
    task_spec_ref: ArtifactRef


class PrepareDependenciesResult(StrictModel):
    dependency_layer_key: str


class VerifyBaselineInput(StrictModel):
    snapshot_ref: ArtifactRef
    dependency_layer_key: str


class DiagnoseCiInput(StrictModel):
    snapshot_ref: ArtifactRef
    task_spec_ref: ArtifactRef
    dependency_layer_key: str = ""


class DiagnoseCiResult(StrictModel):
    diagnosis_ref: ArtifactRef
    baseline_ref: ArtifactRef | None
    dependency_layer_key: str = ""
    repair_eligible: bool
    diagnose_only: bool


class VerificationActivities:
    def __init__(
        self,
        baseline: BaselineVerificationService,
        *,
        sealed: SealedTestBaselineService | None = None,
        candidate: CandidateVerificationService | None = None,
        sandbox_service: SandboxService | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._baseline = baseline
        self._sealed = sealed
        self._candidate = candidate
        self._sandbox = sandbox_service
        self._artifacts = artifact_store

    @activity.defn(name="prepare_dependencies")
    async def prepare_dependencies(
        self, payload: PrepareDependenciesInput
    ) -> PrepareDependenciesResult:
        if self._artifacts is None or self._sandbox is None:
            raise RuntimeError("dependency preparer is not configured")
        caller = ArtifactCaller(
            tenant_id=payload.snapshot_ref.tenant_id,
            run_id=payload.snapshot_ref.run_id,
            role=None,
            service="verification",
        )
        snapshot = RepositorySnapshot.model_validate_json(
            await self._artifacts.get_bytes(payload.snapshot_ref, caller)
        )
        task = TaskSpec.model_validate_json(
            await self._artifacts.get_bytes(payload.task_spec_ref, caller)
        )
        key = await self._sandbox.prepare_dependencies(
            snapshot.source_archive_ref,
            (
                task.dependency_policy.model_copy(update={"include_test_dependencies": True})
                if task.ci_failure
                else task.dependency_policy
            ),
        )
        return PrepareDependenciesResult(dependency_layer_key=key)

    @activity.defn(name="diagnose_ci_failure")
    async def diagnose_ci(self, payload: DiagnoseCiInput) -> DiagnoseCiResult:
        if self._artifacts is None or self._sandbox is None:
            raise RuntimeError("CI diagnosis is not configured")
        caller = ArtifactCaller(
            tenant_id=payload.task_spec_ref.tenant_id,
            run_id=payload.task_spec_ref.run_id,
            role=None,
            service="verification",
        )
        task = TaskSpec.model_validate_json(
            await self._artifacts.get_bytes(payload.task_spec_ref, caller)
        )
        assert task.ci_failure is not None
        key = payload.dependency_layer_key
        if not key:
            snapshot = RepositorySnapshot.model_validate_json(
                await self._artifacts.get_bytes(payload.snapshot_ref, caller)
            )
            try:
                key = await self._sandbox.prepare_dependencies(
                    snapshot.source_archive_ref,
                    task.dependency_policy.model_copy(update={"include_test_dependencies": True}),
                )
            except (RuntimeError, ValueError) as exc:
                from repopilot.services.sandbox_budget import SandboxBudgetExceeded

                if isinstance(exc, SandboxBudgetExceeded):
                    raise
                environment_report = CiDiagnosisReport(
                    category="environment_failure",
                    repair_eligible=False,
                    reasons=(
                        "Dependency build failed; check manifests and supported install policy",
                    ),
                    source=task.ci_failure,
                    evidence_ref=task.ci_failure.evidence_ref,
                )
                ref = await self._artifacts.put_bytes(
                    ArtifactKind.CI_DIAGNOSIS,
                    environment_report.model_dump_json().encode(),
                    ArtifactMetadata(
                        tenant_id=task.tenant_id,
                        run_id=task.run_id,
                        base_revision=task.base_revision,
                        schema_version="1",
                    ),
                )
                return DiagnoseCiResult(
                    diagnosis_ref=ref,
                    baseline_ref=None,
                    repair_eligible=False,
                    diagnose_only=task.ci_failure.mode == "diagnose",
                )
        diagnosis, baseline, eligible = await CiDiagnosisService(
            artifact_store=self._artifacts, sandbox_service=self._sandbox
        ).diagnose(payload.snapshot_ref, task.ci_failure, key)
        return DiagnoseCiResult(
            diagnosis_ref=diagnosis,
            baseline_ref=baseline,
            repair_eligible=eligible,
            diagnose_only=task.ci_failure.mode == "diagnose",
            dependency_layer_key=key,
        )

    @activity.defn(name="verify_baseline")
    async def verify_baseline(self, payload: VerifyBaselineInput) -> ArtifactRef:
        return await self._baseline.verify(
            payload.snapshot_ref,
            dependency_layer_key=payload.dependency_layer_key,
        )

    @activity.defn(name="verify_sealed_tests_on_base")
    async def verify_sealed_tests_on_base(
        self, payload: VerifySealedTestsInput
    ) -> VerifySealedTestsResult:
        if self._sealed is None or self._artifacts is None:
            raise RuntimeError("sealed test verifier 未配置")
        report_ref = await self._sealed.verify(
            snapshot_ref=payload.snapshot_ref,
            test_plan_ref=payload.test_plan_ref,
            dependency_layer_key=payload.dependency_layer_key,
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
        return VerifySealedTestsResult(
            report_ref=report_ref,
            valid=report.valid,
            runnable=report.runnable,
            mismatch_count=len(report.mismatches),
        )

    @activity.defn(name="verify_candidate")
    async def verify_candidate(self, payload: VerifyCandidateInput) -> VerifyCandidateResult:
        if self._candidate is None or self._artifacts is None:
            raise RuntimeError("candidate verifier is not configured")
        caller = ArtifactCaller(
            tenant_id=payload.test_plan_ref.tenant_id,
            run_id=payload.test_plan_ref.run_id,
            role=None,
            service="verification",
        )
        plan = TestPlan.model_validate_json(
            await self._artifacts.get_bytes(payload.test_plan_ref, caller)
        )
        report_ref = await self._candidate.verify(
            snapshot_ref=payload.snapshot_ref,
            baseline_report_ref=payload.baseline_report_ref,
            candidate_source_ref=payload.candidate_source_ref,
            candidate_revision=payload.candidate_revision,
            test_bundle_ref=plan.test_bundle_ref,
            dependency_layer_key=payload.dependency_layer_key,
        )
        report = VerificationReport.model_validate_json(
            await self._artifacts.get_bytes(report_ref, caller)
        )
        return VerifyCandidateResult(report_ref=report_ref, passed=report.passed)

    @activity.defn(name="cleanup_sandboxes")
    async def cleanup_sandboxes(self, run_id: UUID) -> ArtifactRef:
        if self._sandbox is None:
            raise RuntimeError("sandbox cleanup is not configured")
        return await self._sandbox.cleanup_run(run_id)

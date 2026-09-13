"""Real Git/MinIO/PostgreSQL/Docker/Temporal with deterministic model outputs."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.contract.conftest_git import run_git

from repopilot.activities.developer import DeveloperActivities
from repopilot.activities.finalization import FinalizationActivities
from repopilot.activities.ingest import IngestActivities
from repopilot.activities.planning import PlanningActivities
from repopilot.activities.projections import ProjectionActivities
from repopilot.activities.qa import QaAcceptanceMapping, QaActivities, QaSealedTestDesign
from repopilot.activities.repository import RepositoryActivities
from repopilot.activities.reviewer import ReviewerActivities
from repopilot.activities.verification import VerificationActivities
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata
from repopilot.domain.enums import ArtifactKind, RunStatus
from repopilot.domain.errors import ErrorCode
from repopilot.domain.plans import (
    ChangePlan,
    DeveloperFileEdit,
    DeveloperPatchDesign,
    PlannedFileChange,
)
from repopilot.domain.tasks import BudgetInput, CreateRunRequest, FinalReport, RepositoryInput
from repopilot.domain.verification import (
    ReviewDecision,
    SealedTestFile,
    TestCaseSpec,
    VerificationReport,
)
from repopilot.infrastructure.db.model_budget import PostgresModelBudgetStore
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.git.repository_service import GitRepositoryService
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.infrastructure.sandbox.docker import DockerSandboxService
from repopilot.infrastructure.temporal.client import workflow_id_for
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.services.model_gateway import BudgetedModelGateway
from repopilot.services.task_queues import (
    MODEL_TASK_QUEUE,
    ORCHESTRATION_TASK_QUEUE,
    REPOSITORY_TASK_QUEUE,
    SANDBOX_TASK_QUEUE,
)
from repopilot.services.verification_service import (
    BaselineVerificationService,
    CandidateVerificationService,
    SealedTestBaselineService,
)
from repopilot.workflows.code_repair import CodeRepairWorkflow, CodeRepairWorkflowInput


class LocalOriginRepository(GitRepositoryService):
    """Map an API-valid URL to a local fixture origin without a GitHub network call."""

    def __init__(self, *, origin_path: Path, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._origin_path = origin_path

    async def resolve_revision(self, repo_url: str, revision: str | None) -> str:
        return await super().resolve_revision(str(self._origin_path), revision)

    async def _ensure_bare_mirror(self, repo_url: str) -> Path:
        return await super()._ensure_bare_mirror(str(self._origin_path))


@pytest.mark.parametrize(
    ("requires_repair", "budget_exhausted", "preexisting_failure", "unauthorized_edit"),
    [
        (False, False, False, False),
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        (False, False, False, True),
    ],
)
async def test_real_pipeline_outcomes(
    tmp_path: Path,
    artifact_store,
    db_engine,  # type: ignore[no-untyped-def]
    requires_repair: bool,
    budget_exhausted: bool,
    preexisting_failure: bool,
    unauthorized_edit: bool,
) -> None:
    origin = tmp_path / "origin"
    origin.mkdir()
    run_git(["init", "-b", "main"], origin)
    (origin / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    tests_dir = origin / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_base.py").write_text(
        "from calc import add\n\ndef test_api_exists():\n    assert callable(add)\n",
        encoding="utf-8",
    )
    if preexisting_failure:
        (tests_dir / "test_existing_failure.py").write_text(
            "def test_known_problem():\n    assert False\n", encoding="utf-8"
        )
    run_git(["add", "-A"], origin)
    run_git(["commit", "-m", "base"], origin)
    base_revision = run_git(["rev-parse", "HEAD"], origin).strip()
    tenant_id, run_id, item_id = uuid4(), uuid4(), uuid4()
    sessions = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    repository = LocalOriginRepository(
        origin_path=origin,
        data_dir=tmp_path / "repopilot-data",
        artifact_store=artifact_store,
        tenant_id=tenant_id,
    )
    sandbox = DockerSandboxService(
        # Keep the dependency cache between invocations; run sandboxes remain scoped.
        data_dir=Path.cwd() / ".pytest-tmp-e2e-cache",
        artifact_store=artifact_store,
        tenant_id=tenant_id,
    )
    budget = PostgresModelBudgetStore(sessions)
    plan_id = uuid4()
    outputs = [
        ChangePlan(
            plan_id=plan_id,
            version=1,
            supersedes_plan_id=None,
            files=(
                PlannedFileChange(
                    work_item_id=item_id,
                    path="calc.py",
                    operation="modify",
                    owner="developer-1",
                    responsibility="fix addition",
                    required_interfaces=(),
                ),
            ),
            dependency_edges=(),
            risk_flags=(),
        ),
        QaSealedTestDesign(
            files=(
                SealedTestFile(
                    path=".repopilot/sealed_tests/test_acceptance.py",
                    content=(
                        "from calc import add\n\ndef test_adds_numbers():\n"
                        "    assert add(1, 2) == 3\n"
                    ),
                ),
            ),
            cases=(
                TestCaseSpec(
                    name="test_adds_numbers",
                    purpose="acceptance",
                    expected_on_base="fail",
                ),
            ),
            acceptance_mapping=(
                QaAcceptanceMapping(criterion_index=0, test_names=("test_adds_numbers",)),
            ),
        ),
        DeveloperPatchDesign(
            edits=(
                DeveloperFileEdit(
                    path="pyproject.toml" if unauthorized_edit else "calc.py",
                    operation="modify",
                    content=(
                        "def add(a, b):\n    return a * b\n"
                        if requires_repair
                        else "def add(a, b):\n    return a + b\n"
                    ),
                ),
            ),
            rationale="fix arithmetic",
        ),
    ]
    if requires_repair:
        outputs.extend(
            [
                ChangePlan(
                    plan_id=uuid4(),
                    version=2,
                    supersedes_plan_id=plan_id,
                    files=(
                        PlannedFileChange(
                            work_item_id=uuid4(),
                            path="calc.py",
                            operation="modify",
                            owner="developer-1",
                            responsibility="correct failed acceptance test",
                            required_interfaces=(),
                        ),
                    ),
                    dependency_edges=(),
                    risk_flags=(),
                ),
                DeveloperPatchDesign(
                    edits=(
                        DeveloperFileEdit(
                            path="calc.py",
                            operation="modify",
                            content="def add(a, b):\n    return a + b\n",
                        ),
                    ),
                    rationale="repair failed acceptance test",
                ),
            ]
        )
    outputs.append(ReviewDecision(decision="approve", findings=(), rationale="all checks pass"))
    provider = FakeModelProvider(outputs, artifact_store)
    gateway = BudgetedModelGateway(provider, budget)
    model_args = {
        "artifact_store": artifact_store,
        "gateway": gateway,
        "model": "fake-model",
        "reservation_usd": Decimal("0.10"),
    }
    verification = VerificationActivities(
        BaselineVerificationService(artifact_store=artifact_store, sandbox_service=sandbox),
        sealed=SealedTestBaselineService(artifact_store=artifact_store, sandbox_service=sandbox),
        candidate=CandidateVerificationService(
            artifact_store=artifact_store, sandbox_service=sandbox
        ),
        sandbox_service=sandbox,
        artifact_store=artifact_store,
    )
    request = CreateRunRequest(
        repository=RepositoryInput(url="https://github.com/owner/repo"),
        requirement="fix add",
        acceptance_criteria=("add(1, 2) returns 3",),
        budget=BudgetInput(
            max_cost_usd=Decimal("1"),
            max_wall_time_seconds=1800,
            max_model_calls=2 if budget_exhausted else 10,
            max_sandbox_seconds=1800,
        ),
    )
    request_ref = await artifact_store.put_bytes(
        ArtifactKind.CREATE_RUN_REQUEST,
        request.model_dump_json().encode(),
        ArtifactMetadata(
            tenant_id=tenant_id, run_id=run_id, base_revision=None, schema_version="1"
        ),
    )
    await budget.create_budget(
        run_id=run_id,
        tenant_id=tenant_id,
        max_cost_usd=Decimal("1"),
        max_model_calls=2 if budget_exhausted else 10,
    )
    repository_activities = RepositoryActivities(
        artifact_store=artifact_store, repository=repository
    )
    ingest = IngestActivities(artifact_store=artifact_store, repository=repository)
    projection = ProjectionActivities(PostgresRunProjectionStore(sessions))
    finalization = FinalizationActivities(artifact_store=artifact_store, usage_reader=budget)
    async with await WorkflowEnvironment.start_time_skipping(data_converter=data_converter) as env:
        workers = (
            Worker(
                env.client,
                task_queue=ORCHESTRATION_TASK_QUEUE,
                workflows=[CodeRepairWorkflow],
                activities=[projection.update_projection, finalization.build_final_report],
            ),
            Worker(
                env.client,
                task_queue=REPOSITORY_TASK_QUEUE,
                activities=[
                    ingest.ingest_task,
                    ingest.finalize_task_spec,
                    repository_activities.scan_repository,
                    repository_activities.build_developer_context,
                    repository_activities.integrate_patch,
                    repository_activities.export_candidate,
                    repository_activities.build_final_diff,
                    repository_activities.cleanup_repository,
                ],
            ),
            Worker(
                env.client,
                task_queue=SANDBOX_TASK_QUEUE,
                activities=[
                    verification.prepare_dependencies,
                    verification.verify_baseline,
                    verification.verify_sealed_tests_on_base,
                    verification.verify_candidate,
                    verification.cleanup_sandboxes,
                ],
            ),
            Worker(
                env.client,
                task_queue=MODEL_TASK_QUEUE,
                activities=[
                    PlanningActivities(**model_args).plan_change,
                    QaActivities(**model_args).design_sealed_tests,
                    DeveloperActivities(**model_args).develop_patch,
                    ReviewerActivities(**model_args).review_candidate,
                ],
            ),
        )
        async with workers[0], workers[1], workers[2], workers[3]:
            handle = await env.client.start_workflow(
                CodeRepairWorkflow.run,
                CodeRepairWorkflowInput(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    create_request_ref=request_ref,
                ),
                id=workflow_id_for(tenant_id, run_id),
                task_queue=ORCHESTRATION_TASK_QUEUE,
            )
            with env.auto_time_skipping_disabled():
                result = await handle.result()

    assert result.status is (
        RunStatus.FAILED if budget_exhausted or unauthorized_edit else RunStatus.SUCCEEDED
    )
    assert result.final_report_ref is not None
    report = FinalReport.model_validate_json(
        await artifact_store.get_bytes(
            result.final_report_ref,
            ArtifactCaller(tenant_id=tenant_id, run_id=run_id, role=None, service="e2e"),
        )
    )
    assert report.base_revision == base_revision
    if budget_exhausted:
        assert result.failure is not None
        assert result.failure.code is ErrorCode.BUDGET_EXCEEDED
        assert report.model_calls == 2
        assert report.patch_ref is None
        assert report.cleanup_report_ref is not None
        return
    if unauthorized_edit:
        assert result.failure is not None
        assert result.failure.code is ErrorCode.MODEL_OUTPUT_INVALID
        assert report.model_calls == 3
        assert report.patch_ref is None
        assert report.changed_paths == ()
        assert report.cleanup_report_ref is not None
        return
    assert report.changed_paths == ("calc.py",)
    assert report.model_calls == (6 if requires_repair else 4)
    assert report.repair_rounds == (1 if requires_repair else 0)
    assert report.patch_ref is not None
    caller = ArtifactCaller(tenant_id=tenant_id, run_id=run_id, role=None, service="e2e")
    final_diff = (await artifact_store.get_bytes(report.patch_ref, caller)).decode()
    assert "-    return a - b" in final_diff
    assert "+    return a + b" in final_diff
    assert report.verification_ref is not None
    verification_report = VerificationReport.model_validate_json(
        await artifact_store.get_bytes(report.verification_ref, caller)
    )
    assert verification_report.passed
    assert verification_report.candidate_summary.failed == (1 if preexisting_failure else 0)
    assert verification_report.candidate_summary.passed >= 2
    assert verification_report.regression_count == 0
    if preexisting_failure:
        assert any(finding.severity == "minor" for finding in verification_report.findings)
    assert report.review_ref is not None
    assert report.cleanup_report_ref is not None

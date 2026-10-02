"""Recorded CI evidence -> real isolated reproduction -> existing repair workflow.

Git, PostgreSQL, MinIO, Docker and Temporal are real. GitHub and model responses
are deterministic fixtures; this suite verifies integration, not LLM repair rate.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.contract.conftest_git import run_git

from repopilot.activities.blackboard import BlackboardActivities
from repopilot.activities.ci import CiActivities
from repopilot.activities.developer import DeveloperActivities
from repopilot.activities.finalization import FinalizationActivities
from repopilot.activities.ingest import IngestActivities
from repopilot.activities.planning import PlanningActivities
from repopilot.activities.projections import ProjectionActivities
from repopilot.activities.qa import QaAcceptanceMapping, QaActivities, QaSealedTestDesign
from repopilot.activities.repository import RepositoryActivities
from repopilot.activities.reviewer import ReviewerActivities
from repopilot.activities.verification import VerificationActivities
from repopilot.config import get_settings
from repopilot.domain.agents import AgentTurn
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata
from repopilot.domain.ci import CiDiagnosisReport, CiImportSpec
from repopilot.domain.enums import ArtifactKind, RunStatus
from repopilot.domain.plans import ChangePlan, PlannedFileChange
from repopilot.domain.tasks import BudgetInput, CreateRunRequest, FinalReport, RepositoryInput
from repopilot.domain.verification import ReviewDecision, SealedTestFile, TestCaseSpec
from repopilot.infrastructure.db.model_budget import PostgresModelBudgetStore
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.db.sandbox_budget import PostgresSandboxBudgetStore
from repopilot.infrastructure.git.repository_service import GitRepositoryService
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.infrastructure.model.openai_compatible import OpenAICompatibleProvider
from repopilot.infrastructure.sandbox.docker import DockerSandboxService
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.services.ci_source import CiJob, CiRun
from repopilot.services.model_gateway import BudgetedModelGateway
from repopilot.services.sandbox_service import RunCommandRequest, SandboxSpec
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

pytestmark = pytest.mark.e2e
REPO = "https://github.com/owner/formatter"
URL = REPO + "/actions/runs/123/attempts/1"


class LocalRepository(GitRepositoryService):
    def __init__(self, origin, **kwargs):
        super().__init__(**kwargs)
        self.origin = origin

    async def _ensure_bare_mirror(self, repo_url):
        return await super()._ensure_bare_mirror(str(self.origin))


class RecordedCi:
    def __init__(self, sha, log):
        self.sha, self.log = sha, log

    async def inspect(self, run_url):
        return CiRun(
            repository_url=REPO,
            repository_id=1,
            workflow_id=2,
            run_id=123,
            run_attempt=1,
            head_sha=self.sha,
            event="push",
            status="completed",
            conclusion="failure",
            jobs=(CiJob(job_id=7, name="pytest", conclusion="failure", labels=("ubuntu-latest",)),),
        )

    async def job_log(self, run, job_id):
        return self.log


class RecordedRealProvider(OpenAICompatibleProvider):
    """Retain fixture-only model outputs for diagnosing an optional live run."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.raw_responses = []

    async def _post_with_retry(self, payload):
        raw = await super()._post_with_retry(payload)
        self.raw_responses.append(raw)
        return raw


@pytest.mark.parametrize("mode", ["diagnose", "repair"])
async def test_ci_import_real_reproduction_and_repair(
    tmp_path, artifact_store, db_engine, mode, record_property
):
    origin = tmp_path / "origin"
    origin.mkdir()
    run_git(["init", "-b", "main"], origin)
    (origin / "formatter.py").write_text(
        "def normalize_name(name):\n    return name.strip().lower()\n", encoding="utf-8"
    )
    (origin / "tests").mkdir()
    (origin / "tests/test_formatter.py").write_text(
        "from formatter import normalize_name\n"
        "def test_preserves_case():\n    assert normalize_name(' Alice ') == 'Alice'\n"
        "def test_empty():\n    assert normalize_name('  ') == ''\n",
        encoding="utf-8",
    )
    run_git(["add", "-A"], origin)
    run_git(["commit", "-m", "original failing case"], origin)
    sha = run_git(["rev-parse", "HEAD"], origin).strip()
    tenant, run_id, item = uuid4(), uuid4(), uuid4()
    sessions = async_sessionmaker(db_engine, expire_on_commit=False)
    budgets = PostgresSandboxBudgetStore(sessions)
    await budgets.initialize(tenant, run_id, 1800)
    sandbox = DockerSandboxService(
        data_dir=Path.cwd() / ".pytest-tmp-ci-cache",
        artifact_store=artifact_store,
        tenant_id=tenant,
        budget_store=budgets,
    )
    repository = LocalRepository(
        origin, data_dir=tmp_path / "data", artifact_store=artifact_store, tenant_id=tenant
    )
    request = CreateRunRequest(
        repository=RepositoryInput(url=REPO, revision=sha),
        requirement="Import CI; no user-written bug description",
        ci_import=CiImportSpec(run_url=URL, job_id=7, mode=mode),
        acceptance_criteria=("collect CI evidence",),
        budget=BudgetInput(
            max_cost_usd=Decimal("2"),
            max_wall_time_seconds=1800,
            max_model_calls=50,
            max_sandbox_seconds=1800,
        ),
    )
    request_ref = await artifact_store.put_bytes(
        ArtifactKind.CREATE_RUN_REQUEST,
        request.model_dump_json().encode(),
        ArtifactMetadata(tenant_id=tenant, run_id=run_id, base_revision=None, schema_version="1"),
    )
    ingest = IngestActivities(artifact_store=artifact_store, repository=repository)
    task = (await ingest.ingest_task(request_ref)).task_spec_ref
    assert task is not None
    repo_activities = RepositoryActivities(artifact_store=artifact_store, repository=repository)
    snapshot_ref = await repo_activities.scan_repository(task)
    caller = ArtifactCaller(tenant_id=tenant, run_id=run_id, role=None, service="test")
    from repopilot.domain.artifacts import RepositorySnapshot

    snapshot = RepositorySnapshot.model_validate_json(
        await artifact_store.get_bytes(snapshot_ref, caller)
    )
    key = await sandbox.prepare_dependencies(snapshot.source_archive_ref, request.dependency_policy)
    # Produce the recorded CI log using the same real pytest command and oracle.
    sandbox_id = await sandbox.create(
        SandboxSpec(
            run_id=run_id,
            work_item_id=None,
            image="python:3.12-slim",
            source_archive_ref=snapshot.source_archive_ref,
            test_bundle_ref=None,
            dependency_layer_key=key,
        )
    )
    try:
        original = await sandbox.execute(
            sandbox_id,
            RunCommandRequest(
                executable="python",
                args=("-m", "pytest", "-vv", "--tb=short", "tests"),
                cwd="/workspace",
                timeout_seconds=120,
            ),
        )
    finally:
        await sandbox.destroy(sandbox_id)
    assert original.exit_code == 1
    log = (await artifact_store.get_bytes(original.stdout_ref, caller)).decode()
    recorded = RecordedCi(
        sha, f"git log -1 --format=%H\n{sha}\nSuccessfully set up CPython (3.12.8)\n{log}"
    )
    ci = CiActivities(artifact_store, recorded)
    plan = ChangePlan(
        plan_id=uuid4(),
        version=1,
        supersedes_plan_id=None,
        files=(
            PlannedFileChange(
                work_item_id=item,
                path="formatter.py",
                operation="modify",
                owner="developer-1",
                responsibility="Preserve case while trimming whitespace",
                required_interfaces=(),
            ),
        ),
        dependency_edges=(),
        risk_flags=(),
    )
    qa = QaSealedTestDesign(
        files=(
            SealedTestFile(
                path=".repopilot/sealed_tests/test_case.py",
                content=(
                    "from formatter import normalize_name\ndef test_case():\n"
                    "    assert normalize_name(' Bob ') == 'Bob'\n"
                ),
            ),
        ),
        cases=(
            TestCaseSpec(name="test_case", purpose="bug_reproduction", expected_on_base="fail"),
        ),
        acceptance_mapping=tuple(
            QaAcceptanceMapping(criterion_index=i, test_names=("test_case",)) for i in range(3)
        ),
    )
    review = ReviewDecision(
        decision="approve", findings=(), rationale="Original oracle and scope preserved"
    )
    outputs = [
        AgentTurn(tool="submit_plan", arguments={"plan": plan.model_dump(mode="json")}),
        AgentTurn(tool="finish", arguments={"status": "succeeded"}),
        qa,
        AgentTurn(tool="read_file", arguments={"path": "formatter.py"}),
        AgentTurn(
            tool="write_file",
            arguments={
                "path": "formatter.py",
                "content": "def normalize_name(name):\n    return name.strip()\n",
            },
        ),
        AgentTurn(
            tool="run_command",
            arguments={
                "executable": "pytest",
                "args": ["tests", "-q"],
                "cwd": ".",
                "timeout_seconds": 120,
            },
        ),
        AgentTurn(tool="finish", arguments={"status": "succeeded"}),
        AgentTurn(tool="submit_review", arguments={"review": review.model_dump(mode="json")}),
        AgentTurn(tool="finish", arguments={"status": "succeeded"}),
    ]
    model_budget = PostgresModelBudgetStore(sessions)
    await model_budget.create_budget(
        run_id=run_id, tenant_id=tenant, max_cost_usd=Decimal("2"), max_model_calls=50
    )
    provider = FakeModelProvider(outputs, artifact_store)
    real_model = os.environ.get("REPOPILOT_TEST_REAL_MODEL") == "1" and mode == "repair"
    model_name = "fake-model"
    if real_model:
        settings = get_settings()
        assert settings.model_base_url and settings.model_api_key
        provider = RecordedRealProvider(
            base_url=settings.model_base_url,
            api_key=settings.model_api_key.get_secret_value(),
            artifact_store=artifact_store,
            input_usd_per_million_tokens=settings.model_input_usd_per_million_tokens,
            output_usd_per_million_tokens=settings.model_output_usd_per_million_tokens,
            structured_output_mode=settings.model_structured_output_mode,
            reasoning_effort=settings.model_reasoning_effort,
        )
        model_name = settings.model_name
    gateway = BudgetedModelGateway(provider, model_budget)
    args = dict(
        artifact_store=artifact_store,
        gateway=gateway,
        model=model_name,
        reservation_usd=Decimal("0.1"),
    )
    verification = VerificationActivities(
        BaselineVerificationService(artifact_store=artifact_store, sandbox_service=sandbox),
        sealed=SealedTestBaselineService(artifact_store=artifact_store, sandbox_service=sandbox),
        candidate=CandidateVerificationService(
            artifact_store=artifact_store, sandbox_service=sandbox
        ),
        sandbox_service=sandbox,
        artifact_store=artifact_store,
    )
    finalization = FinalizationActivities(
        artifact_store=artifact_store, usage_reader=model_budget, sandbox_budget_store=budgets
    )
    projection = ProjectionActivities(PostgresRunProjectionStore(sessions))
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
                    ci.collect,
                    ingest.ingest_task,
                    repo_activities.scan_repository,
                    repo_activities.build_developer_context,
                    repo_activities.integrate_patch,
                    repo_activities.export_candidate,
                    repo_activities.build_final_diff,
                    repo_activities.cleanup_repository,
                ],
            ),
            Worker(
                env.client,
                task_queue=SANDBOX_TASK_QUEUE,
                activities=[
                    verification.diagnose_ci,
                    verification.verify_sealed_tests_on_base,
                    verification.verify_candidate,
                    verification.cleanup_sandboxes,
                ],
            ),
            Worker(
                env.client,
                task_queue=MODEL_TASK_QUEUE,
                activities=[
                    PlanningActivities(**args).plan_change_with_agent,
                    BlackboardActivities(artifact_store=artifact_store).publish_blackboard,
                    QaActivities(**args).design_sealed_tests,
                    DeveloperActivities(**args, sandbox_service=sandbox).develop_patch_with_agent,
                    ReviewerActivities(**args).review_candidate_with_agent,
                ],
            ),
        )
        async with workers[0], workers[1], workers[2], workers[3]:
            handle = await env.client.start_workflow(
                CodeRepairWorkflow.run,
                CodeRepairWorkflowInput(
                    run_id=run_id,
                    tenant_id=tenant,
                    create_request_ref=request_ref,
                    ci_import_enabled=True,
                    wall_timeout_seconds=1800,
                    developer_agent_mode=True,
                    planner_agent_mode=True,
                    shared_blackboard_enabled=True,
                    reviewer_agent_mode=True,
                ),
                id=f"ci-test-{run_id}",
                task_queue=ORCHESTRATION_TASK_QUEUE,
            )
            with env.auto_time_skipping_disabled():
                result = await handle.result()
    report = FinalReport.model_validate_json(
        await artifact_store.get_bytes(result.final_report_ref, caller)
    )
    if real_model:
        Path(".repopilot").mkdir(exist_ok=True)
        Path(".repopilot/ci-phase1-real-report.json").write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        import json

        Path(".repopilot/ci-phase1-real-model-outputs.json").write_text(
            json.dumps(provider.raw_responses, indent=2), encoding="utf-8"
        )
    assert report.status is RunStatus.SUCCEEDED, report.error
    diagnosis = CiDiagnosisReport.model_validate_json(
        await artifact_store.get_bytes(report.diagnosis_ref, caller)
    )
    assert diagnosis.category == "code_failure" and len(diagnosis.observations) == 2
    assert report.base_revision == sha and report.sandbox_seconds > 0
    assert report.remote_ci_status == "not_verified"
    for key, value in {
        "mode": mode,
        "real_model": real_model,
        "model": model_name,
        "reasoning_effort": settings.model_reasoning_effort if real_model else "fixture",
        "model_calls": report.model_calls,
        "model_cost_usd": str(report.model_cost_usd),
        "sandbox_seconds": report.sandbox_seconds,
        "duration_seconds": report.duration_seconds,
    }.items():
        record_property(key, value)
    if mode == "diagnose":
        assert not provider.requests and report.patch_ref is None
        assert report.local_validation_passed is None
    else:
        assert report.local_validation_passed is True
        assert report.changed_paths == ("formatter.py",)
        patch = await artifact_store.get_bytes(report.patch_ref, caller)
        assert b"return name.strip()" in patch and b"tests/test_formatter.py" not in patch

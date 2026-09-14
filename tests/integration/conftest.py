"""集成测试的公共 fixture：真实 Temporal 测试 Server（时间跳跃）+ 真实
PostgreSQL（testcontainers）+ 真实 Worker。

每个测试函数都拿到一个全新的 WorkflowEnvironment/Worker，虽然比 session
级共享慢一些，但避免了 session 级异步 fixture 和 pytest-asyncio 事件循环
作用域搭配的一堆细节问题——集成测试数量不多，这点开销可以接受。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from testcontainers.community.postgres import PostgresContainer

from repopilot.activities.developer import DevelopAgentPatchInput, DevelopPatchInput
from repopilot.activities.finalization import BuildFinalReportInput
from repopilot.activities.ingest import FinalizeTaskSpecInput
from repopilot.activities.planning import PlanChangeInput, PlanChangeResult
from repopilot.activities.projections import ProjectionActivities
from repopilot.activities.qa import DesignSealedTestsInput
from repopilot.activities.repository import BuildDeveloperContextInput, IntegratePatchInput
from repopilot.activities.reviewer import ReviewCandidateInput, ReviewCandidateResult
from repopilot.activities.verification import (
    PrepareDependenciesInput,
    PrepareDependenciesResult,
    VerifyBaselineInput,
    VerifyCandidateInput,
    VerifyCandidateResult,
    VerifySealedTestsInput,
    VerifySealedTestsResult,
)
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import ArtifactKind, RiskLevel
from repopilot.domain.plans import CandidateSource, PlannedFileChange, WorkItem
from repopilot.domain.tasks import IngestResult
from repopilot.infrastructure.db.models import Base
from repopilot.infrastructure.db.run_projection import PostgresRunProjectionStore
from repopilot.infrastructure.temporal.client import ORCHESTRATION_TASK_QUEUE
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.services.task_queues import (
    MODEL_TASK_QUEUE,
    REPOSITORY_TASK_QUEUE,
    SANDBOX_TASK_QUEUE,
)
from repopilot.workflows.code_repair import CodeRepairWorkflow


def _derived_ref(source: ArtifactRef, kind: ArtifactKind) -> ArtifactRef:
    digest = sha256(f"{source.run_id}:{kind.value}".encode()).hexdigest()
    return ArtifactRef(
        artifact_id=uuid4(),
        run_id=source.run_id,
        tenant_id=source.tenant_id,
        kind=kind,
        schema_version="1",
        object_key=f"fake/{source.run_id}/{kind.value}/{digest}",
        sha256=digest,
        size_bytes=source.size_bytes,
        base_revision="a" * 40,
        input_artifact_ids=(source.artifact_id,),
        created_at=datetime.now(UTC),
    )


def _work_item_ref(
    work_item: WorkItem, task_spec_ref: ArtifactRef, kind: ArtifactKind
) -> ArtifactRef:
    digest = sha256(f"{work_item.work_item_id}:{kind.value}".encode()).hexdigest()
    return ArtifactRef(
        artifact_id=uuid4(),
        run_id=work_item.run_id,
        tenant_id=task_spec_ref.tenant_id,
        kind=kind,
        schema_version="1",
        object_key=f"fake/{work_item.run_id}/{kind.value}/{digest}",
        sha256=digest,
        size_bytes=task_spec_ref.size_bytes,
        base_revision="a" * 40,
        input_artifact_ids=(),
        created_at=datetime.now(UTC),
    )


class FakePipelineActivities:
    def __init__(self) -> None:
        self._last_integrated_ref: ArtifactRef | None = None
        self._verification_attempts: dict[UUID, int] = {}
        self._dependency_prepares: dict[UUID, int] = {}
        self.qa_attempts: dict[UUID, int] = {}
        self.agent_development_calls: dict[UUID, int] = {}

    @activity.defn(name="ingest_task")
    async def ingest_task(self, request_ref: ArtifactRef) -> IngestResult:
        if request_ref.size_bytes == 4:
            raise RuntimeError("synthetic ingest failure: secret must not be echoed")
        if request_ref.size_bytes == 0:
            return IngestResult(
                repository_url="https://github.com/owner/repo",
                base_revision="a" * 40,
                proposed_acceptance_criteria=("human must approve",),
                acceptance_criteria_source="heuristic",
                requires_requirements_approval=True,
                task_spec_ref=None,
            )
        return IngestResult(
            repository_url="https://github.com/owner/repo",
            base_revision="a" * 40,
            proposed_acceptance_criteria=("tests pass",),
            acceptance_criteria_source="structured",
            requires_requirements_approval=False,
            task_spec_ref=_derived_ref(request_ref, ArtifactKind.TASK_SPEC),
        )

    @activity.defn(name="finalize_task_spec")
    async def finalize_task_spec(self, payload: FinalizeTaskSpecInput) -> ArtifactRef:
        return _derived_ref(payload.create_request_ref, ArtifactKind.TASK_SPEC)

    @activity.defn(name="scan_repository")
    async def scan_repository(self, task_ref: ArtifactRef) -> ArtifactRef:
        return _derived_ref(task_ref, ArtifactKind.REPOSITORY_SNAPSHOT)

    @activity.defn(name="prepare_dependencies")
    async def prepare_dependencies(
        self, payload: PrepareDependenciesInput
    ) -> PrepareDependenciesResult:
        run_id = payload.snapshot_ref.run_id
        count = self._dependency_prepares.get(run_id, 0) + 1
        self._dependency_prepares[run_id] = count
        return PrepareDependenciesResult(dependency_layer_key=("b" if count > 1 else "a") * 64)

    @activity.defn(name="verify_baseline")
    async def verify_baseline(self, payload: VerifyBaselineInput) -> ArtifactRef:
        return _derived_ref(payload.snapshot_ref, ArtifactKind.BASELINE_REPORT)

    @activity.defn(name="verify_sealed_tests_on_base")
    async def verify_sealed_tests_on_base(
        self, payload: VerifySealedTestsInput
    ) -> VerifySealedTestsResult:
        qa_attempt = self.qa_attempts.get(payload.test_plan_ref.run_id, 0)
        if payload.test_plan_ref.size_bytes in (8, 9):
            assert qa_attempt > 0
        valid = payload.test_plan_ref.size_bytes != 9 and (
            payload.test_plan_ref.size_bytes != 8 or qa_attempt > 1
        )
        return VerifySealedTestsResult(
            report_ref=_derived_ref(
                payload.test_plan_ref,
                ArtifactKind.SEALED_TEST_BASELINE_REPORT,
            ),
            valid=valid,
            runnable=True,
            mismatch_count=0 if valid else 1,
        )

    @activity.defn(name="plan_change")
    async def plan_change(self, payload: PlanChangeInput) -> PlanChangeResult:
        path = "pyproject.toml" if payload.task_spec_ref.size_bytes == 7 else "src/app.py"
        expands_scope = payload.task_spec_ref.size_bytes == 10 and payload.attempt > 1
        if expands_scope:
            assert payload.allow_scope_expansion
            assert payload.allowed_repair_paths == ("src/app.py",)
        work_item = WorkItem(
            work_item_id=uuid4(),
            run_id=payload.task_spec_ref.run_id,
            kind="code",
            dependencies=(),
            allowed_write_paths=(path, "src/helper.py") if expands_scope else (path,),
            read_paths=(path, "src/helper.py") if expands_scope else (path,),
            owner="developer-1",
            attempt=1,
        )
        return PlanChangeResult(
            plan_ref=_derived_ref(payload.task_spec_ref, ArtifactKind.CHANGE_PLAN),
            risk_level=RiskLevel.MEDIUM if expands_scope else RiskLevel.LOW,
            planned_files=(
                PlannedFileChange(
                    work_item_id=work_item.work_item_id,
                    path=path,
                    operation="modify",
                    owner="developer-1",
                    responsibility="implement task",
                    required_interfaces=(),
                ),
                *(
                    (
                        PlannedFileChange(
                            work_item_id=work_item.work_item_id,
                            path="src/helper.py",
                            operation="create",
                            owner="developer-1",
                            responsibility="new helper required by repair",
                            required_interfaces=(),
                        ),
                    )
                    if expands_scope
                    else ()
                ),
            ),
            work_items=(work_item,),
            waves=((work_item.work_item_id,),),
            scope_expansion_paths=("src/helper.py",) if expands_scope else (),
        )

    @activity.defn(name="design_sealed_tests")
    async def design_sealed_tests(self, payload: DesignSealedTestsInput) -> ArtifactRef:
        run_id = payload.task_spec_ref.run_id
        assert payload.attempt == self.qa_attempts.get(run_id, 0) + 1
        if payload.attempt > 1:
            assert payload.baseline_report_ref is not None
            assert payload.previous_test_plan_ref is not None
            assert payload.sealed_baseline_report_ref is not None
        self.qa_attempts[run_id] = payload.attempt
        return _derived_ref(payload.task_spec_ref, ArtifactKind.TEST_PLAN)

    @activity.defn(name="build_developer_context")
    async def build_developer_context(self, payload: BuildDeveloperContextInput) -> ArtifactRef:
        return _work_item_ref(
            payload.work_item,
            payload.task_spec_ref,
            ArtifactKind.DEVELOPER_CONTEXT,
        )

    @activity.defn(name="develop_patch")
    async def develop_patch(self, payload: DevelopPatchInput) -> ArtifactRef:
        if payload.task_spec_ref.size_bytes == 10 and payload.repair_feedback_ref is not None:
            assert {file.path for file in payload.planned_files} == {
                "src/app.py",
                "src/helper.py",
            }
        return _derived_ref(payload.developer_context_ref, ArtifactKind.PATCH)

    @activity.defn(name="develop_patch_with_agent")
    async def develop_patch_with_agent(self, payload: DevelopAgentPatchInput) -> ArtifactRef:
        run_id = payload.developer_context_ref.run_id
        self.agent_development_calls[run_id] = self.agent_development_calls.get(run_id, 0) + 1
        return _derived_ref(payload.developer_context_ref, ArtifactKind.PATCH)

    @activity.defn(name="integrate_patch")
    async def integrate_patch(self, payload: IntegratePatchInput) -> ArtifactRef:
        self._last_integrated_ref = _derived_ref(
            payload.proposal_ref, ArtifactKind.INTEGRATED_PATCH
        )
        return self._last_integrated_ref

    @activity.defn(name="export_candidate")
    async def export_candidate(self, run_id: UUID) -> CandidateSource:
        assert self._last_integrated_ref is not None
        assert self._last_integrated_ref.run_id == run_id
        return CandidateSource(
            source_archive_ref=_derived_ref(self._last_integrated_ref, ArtifactKind.SOURCE_ARCHIVE),
            revision="b" * 40,
        )

    @activity.defn(name="verify_candidate")
    async def verify_candidate(self, payload: VerifyCandidateInput) -> VerifyCandidateResult:
        run_id = payload.candidate_source_ref.run_id
        attempt = self._verification_attempts.get(run_id, 0) + 1
        self._verification_attempts[run_id] = attempt
        return VerifyCandidateResult(
            report_ref=_derived_ref(payload.candidate_source_ref, ArtifactKind.VERIFICATION_REPORT),
            passed=payload.candidate_source_ref.size_bytes != 2
            and (payload.candidate_source_ref.size_bytes not in (5, 10) or attempt > 1)
            and (
                payload.candidate_source_ref.size_bytes != 7
                or payload.dependency_layer_key == "b" * 64
            ),
        )

    @activity.defn(name="build_final_diff")
    async def build_final_diff(self, run_id: UUID) -> ArtifactRef:
        assert self._last_integrated_ref is not None
        assert self._last_integrated_ref.run_id == run_id
        return _derived_ref(self._last_integrated_ref, ArtifactKind.PATCH)

    @activity.defn(name="review_candidate")
    async def review_candidate(self, payload: ReviewCandidateInput) -> ReviewCandidateResult:
        blocked = payload.diff_ref.size_bytes == 3 or (
            payload.diff_ref.size_bytes == 6 and payload.attempt == 1
        )
        return ReviewCandidateResult(
            review_ref=_derived_ref(payload.diff_ref, ArtifactKind.REVIEW_DECISION),
            decision="request_changes" if blocked else "approve",
        )

    @activity.defn(name="cleanup_repository")
    async def cleanup_repository(self, run_id: UUID) -> ArtifactRef:
        return self._run_ref(run_id, UUID(int=0), ArtifactKind.CLEANUP_REPORT, "repository")

    @activity.defn(name="cleanup_sandboxes")
    async def cleanup_sandboxes(self, run_id: UUID) -> ArtifactRef:
        return self._run_ref(run_id, UUID(int=0), ArtifactKind.CLEANUP_REPORT, "sandbox")

    @activity.defn(name="build_final_report")
    async def build_final_report(self, payload: BuildFinalReportInput) -> ArtifactRef:
        return self._run_ref(
            payload.run_id,
            payload.tenant_id,
            ArtifactKind.FINAL_REPORT,
            payload.status.value,
        )

    @staticmethod
    def _run_ref(
        run_id: UUID,
        tenant_id: UUID,
        kind: ArtifactKind,
        salt: str,
    ) -> ArtifactRef:
        digest = sha256(f"{run_id}:{kind.value}:{salt}".encode()).hexdigest()
        return ArtifactRef(
            artifact_id=uuid4(),
            run_id=run_id,
            tenant_id=tenant_id,
            kind=kind,
            schema_version="1",
            object_key=f"fake/{run_id}/{kind.value}/{digest}",
            sha256=digest,
            size_bytes=1,
            base_revision="a" * 40,
            input_artifact_ids=(),
            created_at=datetime.now(UTC),
        )


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as container:
        yield container


@pytest_asyncio.fixture
async def db_engine(postgres_container: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_container.get_connection_url())
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest.fixture
def fake_pipeline() -> FakePipelineActivities:
    return FakePipelineActivities()


@pytest_asyncio.fixture
async def temporal_client(
    db_engine: AsyncEngine, fake_pipeline: FakePipelineActivities
) -> AsyncIterator[Client]:
    session_factory = async_sessionmaker(bind=db_engine, expire_on_commit=False)
    projection_activities = ProjectionActivities(PostgresRunProjectionStore(session_factory))

    async with await WorkflowEnvironment.start_time_skipping(data_converter=data_converter) as env:
        worker = Worker(
            env.client,
            task_queue=ORCHESTRATION_TASK_QUEUE,
            workflows=[CodeRepairWorkflow],
            activities=[
                projection_activities.update_projection,
                fake_pipeline.build_final_report,
            ],
        )
        repository_worker = Worker(
            env.client,
            task_queue=REPOSITORY_TASK_QUEUE,
            activities=[
                fake_pipeline.ingest_task,
                fake_pipeline.finalize_task_spec,
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
                fake_pipeline.develop_patch_with_agent,
                fake_pipeline.review_candidate,
            ],
        )
        async with worker, repository_worker, sandbox_worker, model_worker:
            yield env.client

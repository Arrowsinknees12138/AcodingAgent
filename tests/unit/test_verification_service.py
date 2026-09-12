"""基线测试发现、执行和报告生成测试。"""

from __future__ import annotations

import io
import tarfile
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    RepositorySnapshot,
)
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.verification import (
    AcceptanceTestMapping,
    BaselineReport,
    SealedTestBaselineReport,
    TestCaseSpec,
    TestPlan,
    TestSummary,
    VerificationReport,
)
from repopilot.services.sandbox_service import (
    CommandResult,
    RunCommandRequest,
    SandboxSpec,
)
from repopilot.services.verification_service import (
    BaselineVerificationService,
    CandidateVerificationService,
    InvalidTestConfigurationError,
    SealedTestBaselineService,
    SourceInventory,
    discover_test_command,
)
from tests.fakes import MemoryArtifactStore


def _snapshot(source_ref: ArtifactRef, symbol_ref: ArtifactRef) -> RepositorySnapshot:
    return RepositorySnapshot(
        repository_url="https://github.com/owner/repo",
        base_revision="a" * 40,
        source_archive_ref=source_ref,
        primary_language="python",
        python_versions=("3.12",),
        dependency_manifest_paths=("pyproject.toml",),
        test_config_paths=(),
        forbidden_paths=(),
        symbol_index_ref=symbol_ref,
    )


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_discover_uses_tests_directory_without_reading_readme() -> None:
    dummy = _dummy_ref(ArtifactKind.SOURCE_ARCHIVE)
    command = discover_test_command(
        _snapshot(dummy, _dummy_ref(ArtifactKind.SYMBOL_INDEX)),
        SourceInventory(paths=("README.md", "tests/test_app.py"), pyproject_content=None),
    )
    assert command.args == ("-m", "pytest", "-q")


def test_explicit_command_rejects_shell_string() -> None:
    dummy = _dummy_ref(ArtifactKind.SOURCE_ARCHIVE)
    pyproject = b'[tool.repopilot]\ntest_command = "pytest -q && curl bad"'
    with pytest.raises(InvalidTestConfigurationError):
        discover_test_command(
            _snapshot(dummy, _dummy_ref(ArtifactKind.SYMBOL_INDEX)),
            SourceInventory(paths=("pyproject.toml",), pyproject_content=pyproject),
        )


class FakeSandbox:
    def __init__(
        self,
        store: MemoryArtifactStore,
        metadata: ArtifactMetadata,
        *,
        stdout: bytes | tuple[bytes, ...] = (
            b"FAILED tests/test_app.py::test_bad - AssertionError\n"
            b"1 failed, 2 passed, 1 skipped in 0.1s"
        ),
        exit_code: int | tuple[int, ...] = 1,
    ) -> None:
        self._store = store
        self._metadata = metadata
        self._stdout = (stdout,) if isinstance(stdout, bytes) else stdout
        self._exit_code = (exit_code,) if isinstance(exit_code, int) else exit_code
        self.commands: list[RunCommandRequest] = []
        self.destroyed = False

    async def create(self, spec: SandboxSpec) -> UUID:
        self.spec = spec
        return uuid4()

    async def execute(self, sandbox_id: UUID, request: RunCommandRequest) -> CommandResult:
        del sandbox_id
        self.command = request
        self.commands.append(request)
        output_index = min(len(self.commands) - 1, len(self._stdout) - 1)
        stdout_ref = await self._store.put_bytes(
            ArtifactKind.LOG,
            self._stdout[output_index],
            self._metadata,
        )
        stderr_ref = await self._store.put_bytes(ArtifactKind.LOG, b"", self._metadata)
        return CommandResult(
            exit_code=self._exit_code[min(len(self.commands) - 1, len(self._exit_code) - 1)],
            timed_out=False,
            oom_killed=False,
            duration_ms=100,
            stdout_ref=stdout_ref,
            stderr_ref=stderr_ref,
        )

    async def export_changes(self, sandbox_id: UUID) -> ArtifactRef:
        raise AssertionError(f"baseline 不应导出变更: {sandbox_id}")

    async def destroy(self, sandbox_id: UUID) -> None:
        del sandbox_id
        self.destroyed = True


async def test_baseline_verification_writes_report_and_always_destroys_sandbox() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        _archive({"tests/test_app.py": b"def test_bad(): assert False"}),
        metadata,
    )
    symbol_ref = await store.put_bytes(ArtifactKind.SYMBOL_INDEX, b"[]", metadata)
    snapshot = _snapshot(source_ref, symbol_ref)
    snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        snapshot.model_dump_json().encode(),
        metadata,
    )
    sandbox = FakeSandbox(store, metadata)
    report_ref = await BaselineVerificationService(
        artifact_store=store,
        sandbox_service=sandbox,
    ).verify(snapshot_ref)
    report = BaselineReport.model_validate_json(
        await store.get_bytes(
            report_ref,
            ArtifactCaller(
                tenant_id=tenant_id,
                run_id=run_id,
                role=None,
                service="test",
            ),
        )
    )
    assert report.runnable is True
    assert report.summary.passed == 2
    assert report.summary.failed == 1
    assert report.summary.skipped == 1
    assert report.summary.failed_test_ids == ("tests/test_app.py::test_bad",)
    assert sandbox.destroyed is True


async def test_candidate_verification_reports_only_new_failures_as_regressions() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        _archive({"tests/test_app.py": b"def test_bad(): assert False"}),
        metadata,
    )
    symbol_ref = await store.put_bytes(ArtifactKind.SYMBOL_INDEX, b"[]", metadata)
    snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        _snapshot(source_ref, symbol_ref).model_dump_json().encode(),
        metadata,
    )
    details_ref = await store.put_bytes(ArtifactKind.LOG, b"baseline", metadata)
    baseline_ref = await store.put_bytes(
        ArtifactKind.BASELINE_REPORT,
        BaselineReport(
            base_revision="a" * 40,
            runnable=True,
            command=("python", "-m", "pytest", "-q"),
            summary=TestSummary(
                passed=2,
                failed=1,
                skipped=0,
                failed_test_ids=("tests/test_app.py::test_known",),
            ),
            details_ref=details_ref,
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    sandbox = FakeSandbox(
        store,
        metadata,
        stdout=(
            (
                b"FAILED tests/test_app.py::test_known - AssertionError\n"
                b"FAILED tests/test_new.py::test_regression - AssertionError\n"
                b"2 failed, 2 passed in 0.1s"
            ),
            b"1 passed in 0.1s",
        ),
        exit_code=(1, 0),
    )

    report_ref = await CandidateVerificationService(
        artifact_store=store,
        sandbox_service=sandbox,
    ).verify(
        snapshot_ref=snapshot_ref,
        baseline_report_ref=baseline_ref,
        candidate_source_ref=source_ref,
        candidate_revision="b" * 40,
        test_bundle_ref=source_ref,
    )
    report = VerificationReport.model_validate_json(
        await store.get_bytes(
            report_ref,
            ArtifactCaller(
                tenant_id=tenant_id,
                run_id=run_id,
                role=None,
                service="test",
            ),
        )
    )
    assert report.passed is False
    assert report.regression_test_ids == ("tests/test_new.py::test_regression",)
    assert report.regression_count == 1
    assert {finding.category for finding in report.findings} == {"test", "regression"}
    assert len(sandbox.commands) == 2
    assert sandbox.commands[1].args == ("-m", "pytest", "-q", ".repopilot/sealed_tests")
    assert sandbox.spec.network_enabled is False
    assert sandbox.destroyed is True


async def test_sealed_test_baseline_accepts_declared_pass_fail_and_skip() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        _archive({"src/app.py": b"VALUE = 1"}),
        metadata,
    )
    symbol_ref = await store.put_bytes(ArtifactKind.SYMBOL_INDEX, b"[]", metadata)
    snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        _snapshot(source_ref, symbol_ref).model_dump_json().encode(),
        metadata,
    )
    bundle_ref = await store.put_bytes(
        ArtifactKind.TEST_BUNDLE,
        _archive({".repopilot/sealed_tests/test_task.py": b""}),
        metadata,
    )
    plan_ref = await store.put_bytes(
        ArtifactKind.TEST_PLAN,
        TestPlan(
            test_plan_id=uuid4(),
            origin="sealed",
            test_bundle_ref=bundle_ref,
            cases=(
                TestCaseSpec(
                    name="test_existing",
                    purpose="regression",
                    expected_on_base="pass",
                ),
                TestCaseSpec(
                    name="test_reproduces_bug",
                    purpose="bug_reproduction",
                    expected_on_base="fail",
                ),
                TestCaseSpec(
                    name="test_optional",
                    purpose="acceptance",
                    expected_on_base="skip",
                ),
            ),
            acceptance_mapping=(
                AcceptanceTestMapping(
                    acceptance_criterion="fixed",
                    test_names=("test_reproduces_bug",),
                ),
            ),
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    sandbox = FakeSandbox(
        store,
        metadata,
        stdout=(
            b".repopilot/sealed_tests/test_task.py::test_existing PASSED [ 33%]\n"
            b".repopilot/sealed_tests/test_task.py::test_reproduces_bug FAILED [ 66%]\n"
            b".repopilot/sealed_tests/test_task.py::test_optional SKIPPED [100%]\n"
            b"1 failed, 1 passed, 1 skipped in 0.1s"
        ),
    )

    report_ref = await SealedTestBaselineService(
        artifact_store=store,
        sandbox_service=sandbox,
    ).verify(snapshot_ref=snapshot_ref, test_plan_ref=plan_ref)
    report = SealedTestBaselineReport.model_validate_json(
        await store.get_bytes(
            report_ref,
            ArtifactCaller(
                tenant_id=tenant_id,
                run_id=run_id,
                role=None,
                service="test",
            ),
        )
    )

    assert report.valid is True
    assert report.case_outcomes == {
        "test_existing": "passed",
        "test_reproduces_bug": "failed",
        "test_optional": "skipped",
    }
    assert report.mismatches == ()
    assert sandbox.spec.test_bundle_ref == bundle_ref
    assert sandbox.spec.network_enabled is False
    assert sandbox.command.args == ("-m", "pytest", "-vv", ".repopilot/sealed_tests")
    assert sandbox.destroyed is True


async def test_sealed_test_baseline_rejects_missing_or_wrong_outcomes() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        _archive({"src/app.py": b"VALUE = 1"}),
        metadata,
    )
    symbol_ref = await store.put_bytes(ArtifactKind.SYMBOL_INDEX, b"[]", metadata)
    snapshot_ref = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        _snapshot(source_ref, symbol_ref).model_dump_json().encode(),
        metadata,
    )
    bundle_ref = await store.put_bytes(ArtifactKind.TEST_BUNDLE, _archive({}), metadata)
    plan_ref = await store.put_bytes(
        ArtifactKind.TEST_PLAN,
        TestPlan(
            test_plan_id=uuid4(),
            origin="sealed",
            test_bundle_ref=bundle_ref,
            cases=(
                TestCaseSpec(
                    name="test_should_fail",
                    purpose="bug_reproduction",
                    expected_on_base="fail",
                ),
                TestCaseSpec(
                    name="test_missing",
                    purpose="acceptance",
                    expected_on_base="pass",
                ),
            ),
            acceptance_mapping=(
                AcceptanceTestMapping(
                    acceptance_criterion="fixed",
                    test_names=("test_should_fail",),
                ),
            ),
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    sandbox = FakeSandbox(
        store,
        metadata,
        stdout=(
            b".repopilot/sealed_tests/test_task.py::test_should_fail PASSED [100%]\n"
            b"1 passed in 0.1s"
        ),
        exit_code=0,
    )

    report_ref = await SealedTestBaselineService(
        artifact_store=store,
        sandbox_service=sandbox,
    ).verify(snapshot_ref=snapshot_ref, test_plan_ref=plan_ref)
    report = SealedTestBaselineReport.model_validate_json(
        await store.get_bytes(
            report_ref,
            ArtifactCaller(
                tenant_id=tenant_id,
                run_id=run_id,
                role=None,
                service="test",
            ),
        )
    )

    assert report.valid is False
    assert report.case_outcomes == {
        "test_should_fail": "passed",
        "test_missing": "missing",
    }
    assert report.mismatches == (
        "test_should_fail: expected failed, observed passed",
        "test_missing: expected passed, observed missing",
    )


def _dummy_ref(kind: ArtifactKind) -> ArtifactRef:
    # 纯函数测试只需要合法 Ref；复用 fake store 的实现需要 await，因此直接构造。
    tenant_id, run_id = uuid4(), uuid4()
    digest = sha256(kind.value.encode()).hexdigest()
    return ArtifactRef(
        artifact_id=uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        kind=kind,
        schema_version="1",
        object_key=f"{tenant_id}/{run_id}/{kind.value}/{digest}",
        sha256=digest,
        size_bytes=0,
        base_revision="a" * 40,
        input_artifact_ids=(),
        created_at=datetime.now(UTC),
    )

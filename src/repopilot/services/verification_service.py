"""基线测试发现与执行（实施设计第 15.1、15.2 节）。"""

from __future__ import annotations

import io
import re
import tarfile
import tomllib
from dataclasses import dataclass
from uuid import uuid4

from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    RepositorySnapshot,
)
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.verification import (
    BaselineReport,
    TestSummary,
    VerificationFinding,
    VerificationReport,
)
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.sandbox_service import (
    CommandResult,
    RunCommandRequest,
    SandboxService,
    SandboxSpec,
)

_PYTEST_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|skipped)")
_PYTEST_FAILED_ID_RE = re.compile(r"^FAILED\s+([^\s]+)", re.MULTILINE)


class TestCommandNotFoundError(RuntimeError):
    pass


class InvalidTestConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class SourceInventory:
    paths: tuple[str, ...]
    pyproject_content: bytes | None


def inspect_source_archive(content: bytes) -> SourceInventory:
    """只读取 tar 索引/pyproject，不把不可信归档解压到宿主文件系统。"""
    paths: list[str] = []
    pyproject_content: bytes | None = None
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
            for member in archive.getmembers():
                normalized = member.name.replace("\\", "/").removeprefix("./")
                if not member.isfile() or not normalized:
                    continue
                paths.append(normalized)
                if normalized == "pyproject.toml":
                    extracted = archive.extractfile(member)
                    if extracted is not None:
                        pyproject_content = extracted.read(1_048_577)
    except tarfile.TarError as exc:
        raise InvalidTestConfigurationError("Source Archive 不是合法 tar.gz") from exc
    if pyproject_content is not None and len(pyproject_content) > 1_048_576:
        raise InvalidTestConfigurationError("pyproject.toml 超过 1 MiB")
    return SourceInventory(paths=tuple(sorted(paths)), pyproject_content=pyproject_content)


def discover_test_command(
    snapshot: RepositorySnapshot,
    inventory: SourceInventory,
    *,
    timeout_seconds: int = 600,
) -> RunCommandRequest:
    """严格按设计顺序发现测试命令，不读取 README 或执行 shell 字符串。"""
    pyproject = _parse_pyproject(inventory.pyproject_content)
    tool = _child_table(pyproject, "tool")
    repopilot = _child_table(tool, "repopilot")
    explicit = repopilot.get("test_command")
    if explicit is not None:
        return _parse_explicit_command(explicit, timeout_seconds)

    pytest_configured = bool(
        {"pytest.ini", "tox.ini"}.intersection(snapshot.test_config_paths) or "pytest" in tool
    )
    has_tests = any(path == "tests" or path.startswith("tests/") for path in inventory.paths)
    if pytest_configured or has_tests:
        return RunCommandRequest(
            executable="python",
            args=("-m", "pytest", "-q"),
            cwd="/workspace",
            timeout_seconds=timeout_seconds,
        )
    raise TestCommandNotFoundError("未发现受支持的测试命令")


class BaselineVerificationService:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        sandbox_service: SandboxService,
        image: str = "python:3.12-slim",
        timeout_seconds: int = 600,
    ) -> None:
        self._artifact_store = artifact_store
        self._sandbox = sandbox_service
        self._image = image
        self._timeout_seconds = timeout_seconds

    async def verify(self, snapshot_ref: ArtifactRef) -> ArtifactRef:
        caller = ArtifactCaller(
            tenant_id=snapshot_ref.tenant_id,
            run_id=snapshot_ref.run_id,
            role=None,
            service="verification",
        )
        snapshot = RepositorySnapshot.model_validate_json(
            await self._artifact_store.get_bytes(snapshot_ref, caller)
        )
        archive_content = await self._artifact_store.get_bytes(snapshot.source_archive_ref, caller)
        command = discover_test_command(
            snapshot,
            inspect_source_archive(archive_content),
            timeout_seconds=self._timeout_seconds,
        )
        sandbox_id = await self._sandbox.create(
            SandboxSpec(
                run_id=snapshot_ref.run_id,
                work_item_id=None,
                image=self._image,
                source_archive_ref=snapshot.source_archive_ref,
                test_bundle_ref=None,
                wall_time_seconds=self._timeout_seconds,
            )
        )
        try:
            result = await self._sandbox.execute(sandbox_id, command)
        finally:
            await self._sandbox.destroy(sandbox_id)

        summary = await self._summarize(result, caller)
        runnable = result.exit_code in {0, 1} and not result.timed_out and not result.oom_killed
        report = BaselineReport(
            base_revision=snapshot.base_revision,
            runnable=runnable,
            command=(command.executable, *command.args),
            summary=summary,
            details_ref=result.stdout_ref,
        )
        return await self._artifact_store.put_bytes(
            ArtifactKind.BASELINE_REPORT,
            report.model_dump_json().encode(),
            ArtifactMetadata(
                tenant_id=snapshot_ref.tenant_id,
                run_id=snapshot_ref.run_id,
                base_revision=snapshot.base_revision,
                schema_version="1",
                input_artifact_ids=(
                    snapshot_ref.artifact_id,
                    result.stdout_ref.artifact_id,
                    result.stderr_ref.artifact_id,
                ),
            ),
        )

    async def _summarize(self, result: CommandResult, caller: ArtifactCaller) -> TestSummary:
        stdout = (await self._artifact_store.get_bytes(result.stdout_ref, caller)).decode(
            "utf-8", errors="replace"
        )
        stderr = (await self._artifact_store.get_bytes(result.stderr_ref, caller)).decode(
            "utf-8", errors="replace"
        )
        combined = f"{stdout}\n{stderr}"
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for count, kind in _PYTEST_COUNT_RE.findall(combined):
            counts[kind] = max(counts[kind], int(count))
        return TestSummary(
            passed=counts["passed"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            failed_test_ids=tuple(dict.fromkeys(_PYTEST_FAILED_ID_RE.findall(combined))),
        )


def _parse_pyproject(content: bytes | None) -> dict[str, object]:
    if content is None:
        return {}
    try:
        parsed = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise InvalidTestConfigurationError("pyproject.toml 无法解析") from exc
    return parsed


def _child_table(parent: dict[str, object], key: str) -> dict[str, object]:
    value = parent.get(key)
    return value if isinstance(value, dict) else {}


def _parse_explicit_command(value: object, timeout_seconds: int) -> RunCommandRequest:
    if not isinstance(value, list) or not value or not all(isinstance(part, str) for part in value):
        raise InvalidTestConfigurationError(
            "tool.repopilot.test_command 必须是非空字符串数组，不能是 shell 字符串"
        )
    executable, *args = value
    if executable not in {"python", "pytest", "ruff", "mypy"}:
        raise InvalidTestConfigurationError(f"不允许的测试 executable: {executable}")
    return RunCommandRequest(
        executable=executable,
        args=tuple(args),
        cwd="/workspace",
        timeout_seconds=timeout_seconds,
    )


class CandidateVerificationService:
    """在全新断网沙箱中验证候选源码，并与基线失败集合比较。"""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        sandbox_service: SandboxService,
        image: str = "python:3.12-slim",
        timeout_seconds: int = 600,
    ) -> None:
        self._artifact_store = artifact_store
        self._sandbox = sandbox_service
        self._image = image
        self._timeout_seconds = timeout_seconds

    async def verify(
        self,
        *,
        snapshot_ref: ArtifactRef,
        baseline_report_ref: ArtifactRef,
        candidate_source_ref: ArtifactRef,
        candidate_revision: str,
        test_bundle_ref: ArtifactRef | None = None,
    ) -> ArtifactRef:
        caller = ArtifactCaller(
            tenant_id=snapshot_ref.tenant_id,
            run_id=snapshot_ref.run_id,
            role=None,
            service="verification",
        )
        snapshot = RepositorySnapshot.model_validate_json(
            await self._artifact_store.get_bytes(snapshot_ref, caller)
        )
        baseline = BaselineReport.model_validate_json(
            await self._artifact_store.get_bytes(baseline_report_ref, caller)
        )
        source = await self._artifact_store.get_bytes(candidate_source_ref, caller)
        command = discover_test_command(
            snapshot,
            inspect_source_archive(source),
            timeout_seconds=self._timeout_seconds,
        )
        sandbox_id = await self._sandbox.create(
            SandboxSpec(
                run_id=snapshot_ref.run_id,
                work_item_id=None,
                image=self._image,
                source_archive_ref=candidate_source_ref,
                test_bundle_ref=test_bundle_ref,
                network_enabled=False,
                wall_time_seconds=self._timeout_seconds,
            )
        )
        try:
            result = await self._sandbox.execute(sandbox_id, command)
        finally:
            await self._sandbox.destroy(sandbox_id)

        candidate_summary = await self._summarize(result, caller)
        baseline_failures = frozenset(baseline.summary.failed_test_ids)
        regressions = tuple(
            test_id
            for test_id in candidate_summary.failed_test_ids
            if test_id not in baseline_failures
        )
        findings = self._build_findings(result, candidate_summary, baseline_failures)
        report = VerificationReport(
            candidate_revision=candidate_revision,
            baseline_report_ref=baseline_report_ref,
            passed=result.exit_code == 0 and not result.timed_out and not result.oom_killed,
            findings=findings,
            baseline_summary=baseline.summary,
            candidate_summary=candidate_summary,
            regression_test_ids=regressions,
            regression_count=len(regressions),
            stdout_ref=result.stdout_ref,
        )
        input_refs = (
            snapshot_ref,
            baseline_report_ref,
            candidate_source_ref,
            test_bundle_ref,
            result.stdout_ref,
            result.stderr_ref,
        )
        return await self._artifact_store.put_bytes(
            ArtifactKind.VERIFICATION_REPORT,
            report.model_dump_json().encode(),
            ArtifactMetadata(
                tenant_id=snapshot_ref.tenant_id,
                run_id=snapshot_ref.run_id,
                base_revision=candidate_revision,
                schema_version="1",
                input_artifact_ids=tuple(ref.artifact_id for ref in input_refs if ref is not None),
            ),
        )

    async def _summarize(self, result: CommandResult, caller: ArtifactCaller) -> TestSummary:
        stdout = (await self._artifact_store.get_bytes(result.stdout_ref, caller)).decode(
            "utf-8", errors="replace"
        )
        stderr = (await self._artifact_store.get_bytes(result.stderr_ref, caller)).decode(
            "utf-8", errors="replace"
        )
        combined = f"{stdout}\n{stderr}"
        counts = {"passed": 0, "failed": 0, "skipped": 0}
        for count, kind in _PYTEST_COUNT_RE.findall(combined):
            counts[kind] = max(counts[kind], int(count))
        return TestSummary(
            passed=counts["passed"],
            failed=counts["failed"],
            skipped=counts["skipped"],
            failed_test_ids=tuple(dict.fromkeys(_PYTEST_FAILED_ID_RE.findall(combined))),
        )

    @staticmethod
    def _build_findings(
        result: CommandResult,
        summary: TestSummary,
        baseline_failures: frozenset[str],
    ) -> tuple[VerificationFinding, ...]:
        findings: list[VerificationFinding] = []
        if result.timed_out:
            findings.append(
                VerificationFinding(
                    finding_id=uuid4(),
                    file_path=None,
                    severity="blocker",
                    category="test",
                    message="候选验证超时",
                )
            )
        if result.oom_killed:
            findings.append(
                VerificationFinding(
                    finding_id=uuid4(),
                    file_path=None,
                    severity="blocker",
                    category="test",
                    message="候选验证超过内存限制",
                )
            )
        for test_id in summary.failed_test_ids:
            is_regression = test_id not in baseline_failures
            findings.append(
                VerificationFinding(
                    finding_id=uuid4(),
                    file_path=test_id.split("::", 1)[0],
                    severity="blocker",
                    category="regression" if is_regression else "test",
                    message=(
                        f"候选新增失败测试: {test_id}"
                        if is_regression
                        else f"候选仍未修复基线失败测试: {test_id}"
                    ),
                )
            )
        if result.exit_code not in {0, None} and not findings:
            findings.append(
                VerificationFinding(
                    finding_id=uuid4(),
                    file_path=None,
                    severity="blocker",
                    category="test",
                    message=f"候选验证命令退出码为 {result.exit_code}，但未解析到测试 ID",
                )
            )
        return tuple(findings)

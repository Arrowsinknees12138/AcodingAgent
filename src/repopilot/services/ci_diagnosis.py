"""CI evidence parsing and reproducibility gate. No LLM decides test outcomes."""

from __future__ import annotations

import hashlib
import io
import re
import tarfile

from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ArtifactRef,
    RepositorySnapshot,
)
from repopilot.domain.ci import CiDiagnosisReport, CiFailureContext, ReproductionObservation
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.verification import BaselineReport, TestSummary
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.sandbox_service import RunCommandRequest, SandboxService, SandboxSpec

CI_TEST_COMMAND = ("python", "-m", "pytest", "-vv", "--tb=short", "tests")
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT\S+\s+", re.MULTILINE)
_OUTCOME = re.compile(
    r"^(tests/.*?::.*?)\s+(PASSED|FAILED|SKIPPED|XFAIL|XPASS|ERROR)\b", re.MULTILINE
)
_FAILURE = re.compile(r"^FAILED\s+(tests/.*?::.*?)\s+-\s+(.+)$", re.MULTILINE)
_ERROR_TYPE = re.compile(r"\b([A-Za-z_][\w.]*?(?:Error|Exception))\b")


def clean_output(text: str) -> str:
    return _TIMESTAMP.sub("", re.sub(r"\x1b\[[0-9;]*m", "", text)).replace("\r", "")


def parse_pytest(text: str) -> tuple[dict[str, str], dict[str, str]]:
    text = clean_output(text)
    outcomes = {node: outcome.lower() for node, outcome in _OUTCOME.findall(text)}
    signatures: dict[str, str] = {}
    for node, reason in _FAILURE.findall(text):
        exception = _ERROR_TYPE.search(reason)
        if exception or reason.startswith("assert "):
            # Keep behavior-bearing message; normalize addresses and workspace prefixes only.
            reason = reason.replace("/workspace/", "")
            reason = re.sub(r"0x[0-9a-f]+", "<address>", reason)
            if len(reason) > 512:
                reason = reason[:512] + " sha256=" + hashlib.sha256(reason.encode()).hexdigest()
            signatures[node] = f"{exception.group(1) if exception else 'AssertionError'}:{reason}"
        outcomes[node] = "failed"
    return outcomes, signatures


def evidence_qualification(
    log: str, labels: tuple[str, ...], event: str, head_sha: str
) -> tuple[str | None, str | None]:
    """Only a demonstrated checkout can pin the tested revision."""
    text = clean_output(log)
    match = re.search(r"git log -1 --format=%H\s*\n([0-9a-f]{40})\b", text)
    tested_sha = match.group(1) if match else None
    if event != "push":
        return tested_sha, "First release supports push runs; PR merge/fork checkout is unsupported"
    if not any(label.startswith("ubuntu-") for label in labels):
        return tested_sha, "Only GitHub hosted Ubuntu jobs are supported"
    if tested_sha is None:
        return None, "Checkout SHA could not be established from job logs"
    if tested_sha != head_sha:
        return tested_sha, "Checkout differs from run head; custom checkout is unsupported"
    if not re.search(r"(?:Successfully set up CPython \(|Python )3\.12(?:\.|\))", text):
        return tested_sha, "Python 3.12 environment is not demonstrated in the job log"
    if re.search(
        r"(?:Initialize containers|Starting service container|docker (?:run|compose))", text
    ):
        return tested_sha, "CI service/container environment cannot be reproduced in this release"
    return tested_sha, None


def protected_files(source: bytes) -> dict[str, str]:
    hashes: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
        for member in archive.getmembers():
            name = member.name.removeprefix("./")
            leaf = name.rsplit("/", 1)[-1]
            protect = (
                name.startswith(("tests/", ".github/"))
                or leaf.startswith("test_")
                or leaf.endswith("_test.py")
                or leaf
                in {
                    "conftest.py",
                    "pytest.ini",
                    "tox.ini",
                    "setup.cfg",
                    "setup.py",
                    "pyproject.toml",
                    "uv.lock",
                    "sitecustomize.py",
                    "usercustomize.py",
                }
                or leaf.startswith("requirements")
                or leaf.endswith(".pth")
            )
            if not protect:
                continue
            if not member.isfile() or ".." in name.split("/") or "\\" in name:
                if member.isdir():
                    continue
                raise ValueError("Unsupported protected archive entry")
            file = archive.extractfile(member)
            if file is None or name in hashes:
                raise ValueError("Invalid or duplicate protected file")
            hashes[name] = hashlib.sha256(file.read()).hexdigest()
    return hashes


def classify(
    source: CiFailureContext, observations: tuple[ReproductionObservation, ...]
) -> CiDiagnosisReport:
    def report(category: str, reason: str, eligible: bool = False) -> CiDiagnosisReport:
        return CiDiagnosisReport.model_validate(
            {
                "category": category,
                "repair_eligible": eligible,
                "reasons": (reason,),
                "source": source,
                "evidence_ref": source.evidence_ref,
                "observations": observations,
            }
        )

    if any(o.exit_code not in {0, 1} or o.timed_out or o.oom_killed for o in observations):
        return report(
            "environment_failure", "pytest could not complete normally in the supported environment"
        )
    targets = set(source.target_test_ids)
    if len(observations) != 2 or any(not targets <= o.outcomes.keys() for o in observations):
        return report(
            "insufficient_evidence", "Original target tests are missing from reproduction results"
        )
    a, b = observations
    if a.outcomes != b.outcomes or a.signatures != b.signatures:
        return report(
            "suspected_flaky", "Two fresh sandbox observations differ; automatic repair stopped"
        )
    if all(a.outcomes[node] == "passed" for node in targets):
        return report("not_reproduced", "CI target failures passed in both local observations")
    if any(a.outcomes[node] != "failed" for node in targets):
        return report(
            "insufficient_evidence",
            "Some original target tests were skipped or had no supported failure",
        )
    if any(a.signatures.get(node) != source.failure_signatures.get(node) for node in targets):
        return report(
            "not_reproduced", "Local failure signatures differ from the original CI failure"
        )
    if any(
        "ModuleNotFoundError:" in a.signatures[node] or "ImportError:" in a.signatures[node]
        for node in targets
    ):
        return report(
            "environment_failure", "Import/dependency failures need environment diagnosis"
        )
    return report(
        "code_failure",
        "Original failure reproduced twice with matching test IDs and signatures",
        True,
    )


class CiDiagnosisService:
    def __init__(self, *, artifact_store: ArtifactStore, sandbox_service: SandboxService) -> None:
        self.artifacts = artifact_store
        self.sandbox = sandbox_service

    async def diagnose(
        self, snapshot_ref: ArtifactRef, source: CiFailureContext, dependency_layer_key: str
    ) -> tuple[ArtifactRef, ArtifactRef, bool]:
        caller = ArtifactCaller(
            tenant_id=snapshot_ref.tenant_id,
            run_id=snapshot_ref.run_id,
            role=None,
            service="verification",
        )
        snapshot = RepositorySnapshot.model_validate_json(
            await self.artifacts.get_bytes(snapshot_ref, caller)
        )
        archive = await self.artifacts.get_bytes(snapshot.source_archive_ref, caller)
        observations: list[ReproductionObservation] = []
        for _ in range(2):
            sandbox_id = await self.sandbox.create(
                SandboxSpec(
                    run_id=snapshot_ref.run_id,
                    work_item_id=None,
                    image="python:3.12-slim",
                    source_archive_ref=snapshot.source_archive_ref,
                    test_bundle_ref=None,
                    dependency_layer_key=dependency_layer_key,
                    network_enabled=False,
                )
            )
            try:
                result = await self.sandbox.execute(
                    sandbox_id,
                    RunCommandRequest(
                        executable="python",
                        args=CI_TEST_COMMAND[1:],
                        cwd="/workspace",
                        timeout_seconds=600,
                    ),
                )
            finally:
                await self.sandbox.destroy(sandbox_id)
            stdout = (await self.artifacts.get_bytes(result.stdout_ref, caller)).decode(
                "utf-8", errors="replace"
            )
            stderr = (await self.artifacts.get_bytes(result.stderr_ref, caller)).decode(
                "utf-8", errors="replace"
            )
            outcomes, signatures = parse_pytest(f"{stdout}\n{stderr}")
            observations.append(
                ReproductionObservation(
                    exit_code=result.exit_code,
                    outcomes=outcomes,
                    signatures=signatures,
                    duration_ms=result.duration_ms,
                    stdout_ref=result.stdout_ref,
                    stderr_ref=result.stderr_ref,
                    timed_out=result.timed_out,
                    oom_killed=result.oom_killed,
                )
            )
        diagnosis = classify(source, tuple(observations)).model_copy(
            update={"protected_hashes": protected_files(archive)}
        )
        metadata = ArtifactMetadata(
            tenant_id=snapshot_ref.tenant_id,
            run_id=snapshot_ref.run_id,
            base_revision=snapshot.base_revision,
            schema_version="1",
            input_artifact_ids=(
                source.evidence_ref.artifact_id,
                snapshot_ref.artifact_id,
                *(ref.artifact_id for o in observations for ref in (o.stdout_ref, o.stderr_ref)),
            ),
        )
        diagnosis_ref = await self.artifacts.put_bytes(
            ArtifactKind.CI_DIAGNOSIS, diagnosis.model_dump_json().encode(), metadata
        )
        outcomes = observations[0].outcomes
        baseline = BaselineReport(
            base_revision=snapshot.base_revision,
            runnable=diagnosis.repair_eligible,
            command=CI_TEST_COMMAND,
            summary=TestSummary(
                passed=sum(v == "passed" for v in outcomes.values()),
                failed=sum(v == "failed" for v in outcomes.values()),
                skipped=sum(v == "skipped" for v in outcomes.values()),
                failed_test_ids=tuple(k for k, v in outcomes.items() if v == "failed"),
            ),
            details_ref=observations[0].stdout_ref,
            diagnosis_ref=diagnosis_ref,
        )
        baseline_ref = await self.artifacts.put_bytes(
            ArtifactKind.BASELINE_REPORT, baseline.model_dump_json().encode(), metadata
        )
        return diagnosis_ref, baseline_ref, diagnosis.repair_eligible

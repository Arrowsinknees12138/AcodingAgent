"""Independent checks for source provenance, triage and original test protection."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from repopilot.activities.ci import CiActivities
from repopilot.api.app import create_app
from repopilot.config import Settings
from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.ci import CiDiagnosisReport, CiFailureContext, CiImportSpec, parse_run_url
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.tasks import BudgetInput, CreateRunRequest, RepositoryInput
from repopilot.domain.verification import VerificationReport
from repopilot.infrastructure.github import GitHubActionsReader, redact_log
from repopilot.services.ci_diagnosis import CiDiagnosisService, evidence_qualification, parse_pytest
from repopilot.services.ci_source import CiJob, CiRun, CiSourceError
from repopilot.services.verification_service import CandidateVerificationService
from tests.fakes import MemoryArtifactStore
from tests.unit.test_run_api import FakeRunControl
from tests.unit.test_verification_service import FakeSandbox, _archive, _snapshot

URL = "https://github.com/owner/repo/actions/runs/123/attempts/2"
NODE = "tests/test_app.py::test_bad"
BAD = (
    f"{NODE} FAILED [ 50%]\ntests/test_app.py::test_ok PASSED [100%]\n"
    f"FAILED {NODE} - assert 1 == 2\n1 failed, 1 passed in 0.1s\n"
)
GOOD = f"{NODE} PASSED [ 50%]\ntests/test_app.py::test_ok PASSED [100%]\n2 passed in 0.1s\n"
LOG = (
    "[command]/usr/bin/git log -1 --format=%H\n"
    + "a" * 40
    + "\nSuccessfully set up CPython (3.12.8)\n"
    + BAD
)


class Reader:
    async def inspect(self, url):
        return CiRun(
            repository_url="https://github.com/owner/repo",
            repository_id=1,
            workflow_id=2,
            run_id=123,
            run_attempt=2,
            head_sha="a" * 40,
            event="push",
            status="completed",
            conclusion="failure",
            jobs=(CiJob(job_id=7, name="tests", conclusion="failure", labels=("ubuntu-latest",)),),
        )

    async def job_log(self, run, job_id):
        return LOG


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b/actions/runs/1",
        "https://evil.com/a/b/actions/runs/1",
        "https://github.com/../b/actions/runs/1",
        "https://github.com/a/b/actions/runs/1?x=1",
    ],
)
def test_run_url_rejects_untrusted_or_ambiguous_urls(url):
    with pytest.raises(ValueError):
        parse_run_url(url)


def test_timestamped_pytest_and_assertion_without_exception_name():
    timestamped = "\n".join("2026-10-02T10:00:00.000Z " + line for line in BAD.splitlines())
    outcomes, signatures = parse_pytest(timestamped)
    assert outcomes[NODE] == "failed"
    assert signatures[NODE] == "AssertionError:assert 1 == 2"


@pytest.mark.parametrize(
    "labels,event,log,expected",
    [
        (("ubuntu-latest",), "push", LOG, None),
        (("windows-latest",), "push", LOG, "Ubuntu"),
        (("ubuntu-latest",), "pull_request", LOG, "PR"),
        (("ubuntu-latest",), "push", LOG.replace("3.12.8", "3.11.8"), "Python"),
        (("ubuntu-latest",), "push", LOG.replace("a" * 40, "b" * 40), "Checkout differs"),
        (("ubuntu-latest",), "push", BAD, "Checkout SHA"),
    ],
)
def test_environment_qualification(labels, event, log, expected):
    _, reason = evidence_qualification(log, labels, event, "a" * 40)
    assert reason is None if expected is None else expected in reason


async def setup_case(outputs=(BAD, BAD), exit_codes=(1, 1)):
    store = MemoryArtifactStore()
    metadata = ArtifactMetadata(
        tenant_id=uuid4(), run_id=uuid4(), base_revision="a" * 40, schema_version="1"
    )
    evidence = await store.put_bytes(ArtifactKind.CI_EVIDENCE, LOG.encode(), metadata)
    context = CiFailureContext(
        run_url=URL,
        repository_id=1,
        workflow_id=2,
        run_id=123,
        run_attempt=2,
        job_id=7,
        job_name="tests",
        head_sha="a" * 40,
        tested_sha="a" * 40,
        target_test_ids=(NODE,),
        failure_signatures=parse_pytest(BAD)[1],
        evidence_ref=evidence,
        mode="repair",
    )
    archive = _archive({"app.py": b"def app(): return 1", "tests/test_app.py": b"original oracle"})
    source = await store.put_bytes(ArtifactKind.SOURCE_ARCHIVE, archive, metadata)
    symbols = await store.put_bytes(ArtifactKind.SYMBOL_INDEX, b"[]", metadata)
    snapshot = await store.put_bytes(
        ArtifactKind.REPOSITORY_SNAPSHOT,
        _snapshot(source, symbols).model_dump_json().encode(),
        metadata,
    )
    sandbox = FakeSandbox(
        store, metadata, stdout=tuple(o.encode() for o in outputs), exit_code=exit_codes
    )
    diagnosis, baseline, eligible = await CiDiagnosisService(
        artifact_store=store, sandbox_service=sandbox
    ).diagnose(snapshot, context, "layer")
    return store, metadata, snapshot, diagnosis, baseline, eligible


@pytest.mark.parametrize(
    "outputs,codes,category",
    [
        ((BAD, BAD), (1, 1), "code_failure"),
        ((BAD, GOOD), (1, 0), "suspected_flaky"),
        ((GOOD, GOOD), (0, 0), "not_reproduced"),
        (("missing pytest", "missing pytest"), (2, 2), "environment_failure"),
        (("", ""), (0, 0), "insufficient_evidence"),
        ((BAD.replace("1 == 2", "7 == 8"),) * 2, (1, 1), "not_reproduced"),
    ],
)
async def test_reproduction_gate(outputs, codes, category):
    store, _, _, diagnosis, _, eligible = await setup_case(outputs, codes)
    report = CiDiagnosisReport.model_validate_json(store.content[diagnosis.artifact_id])
    assert report.category == category
    assert eligible is (category == "code_failure")
    assert len(report.observations) == 2
    assert "tests/test_app.py" in report.protected_hashes


@pytest.mark.parametrize(
    "output,code,expected",
    [
        (GOOD, 0, True),
        (BAD, 1, False),
        (GOOD.replace(f"{NODE} PASSED", f"{NODE} SKIPPED"), 0, False),
        ("tests/test_app.py::test_ok PASSED [100%]\n1 passed", 0, False),
        (GOOD.replace("test_ok PASSED", "test_ok FAILED"), 1, False),
    ],
)
async def test_candidate_cannot_leave_skip_or_drop_original_failure(output, code, expected):
    store, metadata, snapshot, _, baseline, _ = await setup_case()
    source = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        _archive({"app.py": b"def app(): return 2", "tests/test_app.py": b"original oracle"}),
        metadata,
    )
    sandbox = FakeSandbox(store, metadata, stdout=output.encode(), exit_code=code)
    report_ref = await CandidateVerificationService(
        artifact_store=store, sandbox_service=sandbox
    ).verify(
        snapshot_ref=snapshot,
        baseline_report_ref=baseline,
        candidate_source_ref=source,
        candidate_revision="b" * 40,
    )
    report = VerificationReport.model_validate_json(store.content[report_ref.artifact_id])
    assert report.passed is expected
    assert sandbox.commands[0].args == ("-m", "pytest", "-vv", "--tb=short", "tests")


async def test_changed_original_test_is_rejected_before_execution():
    store, metadata, snapshot, _, baseline, _ = await setup_case()
    candidate = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE, _archive({"tests/test_app.py": b"assert True"}), metadata
    )
    sandbox = FakeSandbox(store, metadata, stdout=GOOD.encode(), exit_code=0)
    ref = await CandidateVerificationService(artifact_store=store, sandbox_service=sandbox).verify(
        snapshot_ref=snapshot,
        baseline_report_ref=baseline,
        candidate_source_ref=candidate,
        candidate_revision="b" * 40,
    )
    assert not VerificationReport.model_validate_json(store.content[ref.artifact_id]).passed
    assert not sandbox.commands


async def test_collection_freezes_evidence_and_builds_task_without_bug_text():
    store = MemoryArtifactStore()
    metadata = ArtifactMetadata(
        tenant_id=uuid4(), run_id=uuid4(), base_revision=None, schema_version="1"
    )
    request = CreateRunRequest(
        repository=RepositoryInput(url="https://github.com/owner/repo"),
        requirement="Import CI",
        ci_import=CiImportSpec(run_url=URL, job_id=7),
        budget=BudgetInput.model_validate_json(
            '{"max_cost_usd":5,"max_wall_time_seconds":600,"max_model_calls":20,"max_sandbox_seconds":120}'
        ),
    )
    ref = await store.put_bytes(
        ArtifactKind.CREATE_RUN_REQUEST, request.model_dump_json().encode(), metadata
    )
    result = await CiActivities(store, Reader()).collect(ref)
    resolved = CreateRunRequest.model_validate_json(store.content[result.request_ref.artifact_id])
    assert result.ready and resolved.repository.revision == "a" * 40
    assert resolved.ci_failure.target_test_ids == (NODE,)
    assert resolved.acceptance_criteria


def test_import_auth_and_duplicate_source_identity():
    control = FakeRunControl()
    client = TestClient(create_app(run_control=control, ci_reader=Reader(), settings=Settings()))
    body = {"run_url": URL}
    assert client.post("/v1/ci/import", json=body).status_code == 401
    headers = {"Authorization": "Bearer local-dev-token"}
    assert client.post("/v1/ci/import", json=body, headers=headers).status_code == 202
    assert client.post("/v1/ci/import", json=body, headers=headers).status_code == 202
    assert control.keys[0] == control.keys[1]
    assert control.requests[0].ci_import.job_id == 7
    assert (
        client.post("/v1/ci/import", json=body | {"job_id": 99}, headers=headers).status_code == 422
    )


async def test_log_download_does_not_forward_token_and_redacts_secrets():
    reader = Reader()
    run = await reader.inspect(URL)

    def handler(request):
        if request.url.host == "api.github.com":
            assert request.headers["Authorization"] == "Bearer sensitive"
            return httpx.Response(
                302, headers={"Location": "https://logs.blob.core.windows.net/test"}
            )
        assert "Authorization" not in request.headers
        return httpx.Response(200, text="api_key=secretvalue ghp_sensitiveexample")

    result = await GitHubActionsReader(
        token="sensitive", transport=httpx.MockTransport(handler)
    ).job_log(run, 7)
    assert "secretvalue" not in result and "ghp_sensitiveexample" not in result


async def test_log_download_rejects_non_github_redirect():
    run = await Reader().inspect(URL)
    reader = GitHubActionsReader(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"Location": "http://127.0.0.1/secrets"})
        )
    )
    with pytest.raises(CiSourceError, match="Untrusted"):
        await reader.job_log(run, 7)


def test_log_redaction():
    assert "private" not in redact_log("Authorization: Bearer private\npassword=private")

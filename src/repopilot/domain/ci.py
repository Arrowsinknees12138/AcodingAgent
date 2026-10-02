"""CI import provenance and deterministic diagnosis contracts."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef

_RUN_URL = re.compile(
    r"https://github\.com/([\w.-]+)/([\w.-]+)/actions/runs/([1-9]\d*)"
    r"(?:/attempts/([1-9]\d*))?/?$"
)

DiagnosisCategory = Literal[
    "code_failure",
    "environment_failure",
    "suspected_flaky",
    "not_reproduced",
    "insufficient_evidence",
    "unsupported_environment",
    "platform_failure",
]


def parse_run_url(url: str) -> tuple[str, str, int, int | None]:
    match = _RUN_URL.fullmatch(url)
    if match is None or any(part in {".", ".."} for part in match.groups()[:2]):
        raise ValueError("需要 https://github.com/owner/repo/actions/runs/ID[/attempts/N]")
    owner, repo, run, attempt = match.groups()
    return owner, repo, int(run), int(attempt) if attempt else None


class CiImportSpec(StrictModel):
    run_url: str
    job_id: int | None = Field(default=None, gt=0)
    mode: Literal["diagnose", "repair"] = "diagnose"

    @field_validator("run_url")
    @classmethod
    def _valid_run(cls, value: str) -> str:
        parse_run_url(value)
        return value.rstrip("/")


class CiFailureContext(StrictModel):
    run_url: str
    repository_id: int
    workflow_id: int
    run_id: int
    run_attempt: int
    job_id: int
    job_name: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    tested_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_test_ids: tuple[str, ...]
    failure_signatures: dict[str, str]
    evidence_ref: ArtifactRef
    mode: Literal["diagnose", "repair"]

    @field_validator("target_test_ids")
    @classmethod
    def _safe_nodes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) > 100:
            raise ValueError("CI requires 1..100 target test IDs")
        for value in values:
            path = value.split("::", 1)[0]
            if (
                not path.startswith("tests/")
                or not path.endswith(".py")
                or ".." in path.split("/")
                or "\\" in value
                or any(ord(c) < 32 for c in value)
                or len(value) > 1000
            ):
                raise ValueError("Only pytest node IDs under tests/ are supported")
        return values


class ReproductionObservation(StrictModel):
    exit_code: int | None
    outcomes: dict[str, str]
    signatures: dict[str, str]
    duration_ms: int
    stdout_ref: ArtifactRef
    stderr_ref: ArtifactRef
    timed_out: bool = False
    oom_killed: bool = False


class CiDiagnosisReport(StrictModel):
    category: DiagnosisCategory
    repair_eligible: bool
    reasons: tuple[str, ...]
    source: CiFailureContext | None = None
    evidence_ref: ArtifactRef | None = None
    observations: tuple[ReproductionObservation, ...] = ()
    protected_hashes: dict[str, str] = Field(default_factory=dict)
    verification_scope: str = "Linux / Python 3.12 / pytest tests/; local validation only"
    remote_ci_status: Literal["not_verified"] = "not_verified"

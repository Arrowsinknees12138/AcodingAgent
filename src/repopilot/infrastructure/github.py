"""Read-only GitHub Actions adapter with fixed hosts and bounded downloads."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from urllib.parse import urlsplit

import httpx

from repopilot.domain.ci import parse_run_url
from repopilot.services.ci_source import CiJob, CiRun, CiSourceError

MAX_LOG_BYTES = 3 * 1024 * 1024


def redact_log(text: str) -> str:
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    text = re.sub(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+|sk-[A-Za-z0-9_-]+)\b",
        "[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(authorization\s*[:=]\s*(?:bearer\s+|basic\s+)?)[^\s]+", r"\1[REDACTED]", text
    )
    return re.sub(
        r"(?i)((?:token|password|secret|api[_-]?key)\s*[:=]\s*)[^\s]+", r"\1[REDACTED]", text
    )


class GitHubActionsReader:
    def __init__(
        self, *, token: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._token = token
        self._transport = transport

    def _client(self, *, authenticated: bool = True) -> httpx.AsyncClient:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if authenticated and self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return httpx.AsyncClient(
            headers=headers, timeout=20, follow_redirects=False, transport=self._transport
        )

    @staticmethod
    async def _json(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
        response = await client.get(f"https://api.github.com{path}")
        if response.status_code != 200:
            raise CiSourceError(f"GitHub metadata unavailable (HTTP {response.status_code})")
        value = response.json()
        if not isinstance(value, dict):
            raise CiSourceError("Invalid GitHub metadata")
        return value

    async def inspect(self, run_url: str) -> CiRun:
        owner, repo, run_id, requested_attempt = parse_run_url(run_url)
        prefix = f"/repos/{owner}/{repo}"
        try:
            async with asyncio.timeout(45), self._client() as client:
                repository = await self._json(client, prefix)
                if repository.get("private") is not False:
                    raise CiSourceError("Only public repositories are supported")
                run = await self._json(client, f"{prefix}/actions/runs/{run_id}")
                attempt = requested_attempt or int(run["run_attempt"])
                run = await self._json(client, f"{prefix}/actions/runs/{run_id}/attempts/{attempt}")
                if int(run["id"]) != run_id or int(run["run_attempt"]) != attempt:
                    raise CiSourceError("GitHub run identity mismatch")
                if int(run["repository"]["id"]) != int(repository["id"]):
                    raise CiSourceError("GitHub repository identity mismatch")
                jobs: list[CiJob] = []
                for page in range(1, 11):
                    body = await self._json(
                        client,
                        f"{prefix}/actions/runs/{run_id}/attempts/{attempt}/jobs"
                        f"?per_page=100&page={page}",
                    )
                    for job in body["jobs"]:
                        if int(job["run_id"]) != run_id:
                            raise CiSourceError("GitHub job identity mismatch")
                        jobs.append(
                            CiJob(
                                job_id=int(job["id"]),
                                name=str(job["name"]),
                                conclusion=job.get("conclusion"),
                                labels=tuple(job.get("labels", [])),
                            )
                        )
                    if len(jobs) >= int(body["total_count"]):
                        break
                else:
                    raise CiSourceError("Too many CI jobs; choose a smaller workflow")
                return CiRun(
                    repository_url=f"https://github.com/{owner}/{repo}",
                    repository_id=int(repository["id"]),
                    workflow_id=int(run["workflow_id"]),
                    run_id=run_id,
                    run_attempt=attempt,
                    head_sha=str(run["head_sha"]),
                    event=str(run["event"]),
                    status=str(run["status"]),
                    conclusion=run.get("conclusion"),
                    jobs=tuple(jobs),
                )
        except (httpx.HTTPError, TimeoutError, KeyError, TypeError, ValueError) as exc:
            raise CiSourceError("GitHub metadata could not be read; check access or retry") from exc

    async def job_log(self, run: CiRun, job_id: int) -> str:
        if job_id not in {job.job_id for job in run.jobs}:
            raise CiSourceError("Job does not belong to the selected run attempt")
        path = run.repository_url.removeprefix("https://github.com")
        try:
            async with asyncio.timeout(45), self._client() as client:
                response = await client.get(
                    f"https://api.github.com/repos{path}/actions/jobs/{job_id}/logs"
                )
                if response.status_code != 302:
                    raise CiSourceError(f"CI logs unavailable (HTTP {response.status_code})")
                location = response.headers.get("location", "")
                url = urlsplit(location)
                host = url.hostname or ""
                if (
                    url.scheme != "https"
                    or url.username
                    or url.password
                    or url.port not in {None, 443}
                    or not (
                        host.endswith(".blob.core.windows.net")
                        or host.endswith(".actions.githubusercontent.com")
                    )
                ):
                    raise CiSourceError("Untrusted GitHub log download location")
                # Never forward GitHub authorization to the signed download host.
                async with self._client(authenticated=False) as download:
                    async with download.stream("GET", location) as stream:
                        if stream.status_code != 200:
                            raise CiSourceError(f"CI logs unavailable (HTTP {stream.status_code})")
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in stream.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_LOG_BYTES:
                                raise CiSourceError("CI log exceeds the 3 MiB limit")
                            chunks.append(chunk)
                        return redact_log(b"".join(chunks).decode("utf-8", errors="replace"))
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            raise CiSourceError("CI log download failed; logs may have expired") from exc

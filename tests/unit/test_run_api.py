"""Run 控制面 HTTP 契约，不连接数据库、MinIO 或 Temporal。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from repopilot.api.app import create_app
from repopilot.config import Settings
from repopilot.domain.enums import RunStatus
from repopilot.domain.tasks import CreateRunRequest
from repopilot.services.run_control import (
    ArtifactDownload,
    ArtifactView,
    RunEventView,
    RunNotFoundError,
    RunView,
)
from repopilot.workflows.updates import ApprovalRequest


class FakeRunControl:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.approvals: list[ApprovalRequest] = []
        self.cancelled: list[UUID] = []
        self.keys: list[str] = []
        self.artifact_id = uuid4()

    async def create(self, request: CreateRunRequest, *, idempotency_key: str) -> RunView:
        del request
        self.keys.append(idempotency_key)
        return self._view()

    async def get(self, run_id: UUID) -> RunView:
        if run_id != self.run_id:
            raise RunNotFoundError("missing")
        return self._view()

    async def approve(self, run_id: UUID, approval: ApprovalRequest) -> None:
        if run_id != self.run_id:
            raise RunNotFoundError("missing")
        self.approvals.append(approval)

    async def cancel(self, run_id: UUID) -> None:
        if run_id != self.run_id:
            raise RunNotFoundError("missing")
        self.cancelled.append(run_id)

    async def list_events(self, run_id: UUID) -> tuple[RunEventView, ...]:
        if run_id != self.run_id:
            raise RunNotFoundError("missing")
        return (
            RunEventView(
                event_id=uuid4(),
                event_type="run.status_changed",
                actor_type="workflow",
                actor_id=None,
                payload={"status": "queued", "sequence": 0},
                created_at=datetime.now(UTC),
            ),
        )

    async def list_artifacts(self, run_id: UUID) -> tuple[ArtifactView, ...]:
        if run_id != self.run_id:
            raise RunNotFoundError("missing")
        return (self._artifact(),)

    async def download_artifact(self, artifact_id: UUID) -> ArtifactDownload:
        if artifact_id != self.artifact_id:
            raise RunNotFoundError("missing")
        return ArtifactDownload(artifact=self._artifact(), content=b"artifact-data")

    def _view(self) -> RunView:
        return RunView(
            run_id=self.run_id,
            workflow_id=f"repopilot/test/{self.run_id}",
            status=RunStatus.QUEUED,
        )

    def _artifact(self) -> ArtifactView:
        return ArtifactView(
            artifact_id=self.artifact_id,
            run_id=self.run_id,
            kind="task_spec",
            schema_version="1",
            sha256="a" * 64,
            size_bytes=13,
            base_revision="b" * 40,
            created_at=datetime.now(UTC),
        )


def _client(control: FakeRunControl) -> TestClient:
    settings = Settings(api_token="test-token")
    return TestClient(create_app(run_control=control, settings=settings))


def _request_body() -> dict[str, object]:
    return {
        "repository": {"url": "https://github.com/owner/repo", "revision": None},
        "requirement": "fix the bug",
        "acceptance_criteria": ["tests pass"],
        "budget": {
            "max_cost_usd": 2,
            "max_wall_time_seconds": 600,
            "max_model_calls": 10,
            "max_sandbox_seconds": 300,
        },
    }


def test_create_run_requires_authentication_and_idempotency_key() -> None:
    control = FakeRunControl()
    client = _client(control)
    assert client.post("/v1/runs", json=_request_body()).status_code == 401

    idempotency_key = str(uuid4())
    response = client.post(
        "/v1/runs",
        json=_request_body(),
        headers={"Authorization": "Bearer test-token", "Idempotency-Key": idempotency_key},
    )
    assert response.status_code == 202
    assert response.json()["run_id"] == str(control.run_id)
    assert control.keys == [idempotency_key]


def test_get_approve_and_cancel_run() -> None:
    control = FakeRunControl()
    client = _client(control)
    headers = {"Authorization": "Bearer test-token"}

    assert client.get(f"/v1/runs/{control.run_id}", headers=headers).status_code == 200
    approval = client.post(
        f"/v1/runs/{control.run_id}/approvals",
        headers=headers,
        json={
            "kind": "plan",
            "decision": "approve",
            "reason": "reviewed",
        },
    )
    assert approval.status_code == 204
    assert control.approvals[0].kind == "plan"
    assert control.approvals[0].actor_id == "local-admin"

    cancellation = client.post(f"/v1/runs/{control.run_id}:cancel", headers=headers)
    assert cancellation.status_code == 202
    assert control.cancelled == [control.run_id]


def test_unknown_run_returns_404() -> None:
    response = _client(FakeRunControl()).get(
        f"/v1/runs/{uuid4()}", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 404


def test_events_artifacts_and_download_are_exposed() -> None:
    control = FakeRunControl()
    client = _client(control)
    headers = {"Authorization": "Bearer test-token"}

    events = client.get(f"/v1/runs/{control.run_id}/events", headers=headers)
    artifacts = client.get(f"/v1/runs/{control.run_id}/artifacts", headers=headers)
    download = client.get(f"/v1/artifacts/{control.artifact_id}/download", headers=headers)

    assert events.status_code == 200
    assert events.json()[0]["payload"]["status"] == "queued"
    assert artifacts.status_code == 200
    assert artifacts.json()[0]["artifact_id"] == str(control.artifact_id)
    assert download.status_code == 200
    assert download.content == b"artifact-data"
    assert download.headers["etag"] == "a" * 64

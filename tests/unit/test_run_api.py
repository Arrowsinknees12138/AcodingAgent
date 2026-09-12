"""Run 控制面 HTTP 契约，不连接数据库、MinIO 或 Temporal。"""

from __future__ import annotations

from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from repopilot.api.app import create_app
from repopilot.config import Settings
from repopilot.domain.enums import RunStatus
from repopilot.domain.tasks import CreateRunRequest
from repopilot.services.run_control import RunNotFoundError, RunView
from repopilot.workflows.updates import ApprovalRequest


class FakeRunControl:
    def __init__(self) -> None:
        self.run_id = uuid4()
        self.approvals: list[ApprovalRequest] = []
        self.cancelled: list[UUID] = []
        self.keys: list[str] = []

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

    def _view(self) -> RunView:
        return RunView(
            run_id=self.run_id,
            workflow_id=f"repopilot/test/{self.run_id}",
            status=RunStatus.QUEUED,
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
            "actor_id": "human-1",
            "reason": "reviewed",
        },
    )
    assert approval.status_code == 204
    assert control.approvals[0].kind == "plan"

    cancellation = client.post(f"/v1/runs/{control.run_id}:cancel", headers=headers)
    assert cancellation.status_code == 202
    assert control.cancelled == [control.run_id]


def test_unknown_run_returns_404() -> None:
    response = _client(FakeRunControl()).get(
        f"/v1/runs/{uuid4()}", headers={"Authorization": "Bearer test-token"}
    )
    assert response.status_code == 404

"""健康检查路由的单元测试：只测路由行为，不依赖任何外部服务。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from repopilot.api.app import app, create_app


class FakeReadinessProbe:
    def __init__(self, ready: bool) -> None:
        self._ready = ready

    async def check(self) -> bool:
        return self._ready


def test_live_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_ok() -> None:
    client = TestClient(create_app(readiness_probe=FakeReadinessProbe(True)))
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_503_when_database_is_unavailable() -> None:
    client = TestClient(create_app(readiness_probe=FakeReadinessProbe(False)))
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}

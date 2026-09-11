"""健康检查路由的单元测试：只测路由行为，不依赖任何外部服务。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from repopilot.api.app import app


def test_live_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

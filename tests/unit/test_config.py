"""Settings 解析与安全约束的单元测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from repopilot.config import Settings


def test_defaults_do_not_require_env_file() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.env == "dev"
    assert settings.data_dir.is_absolute()


def test_sandbox_url_rejects_non_loopback() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sandbox_service_url="http://0.0.0.0:8091")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost.evil.example:8091",
        "http://127.0.0.1.evil.example:8091",
        "http://localhost@evil.example:8091",
        "http://127.0.0.1:8091@evil.example",
        "http://localhost:99999",
        "http://localhost:8091/redirect",
        "http://localhost:8091?target=evil",
        "http://localhost:8091#fragment",
    ],
)
def test_sandbox_url_rejects_prefix_and_authority_tricks(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, sandbox_service_url=url)  # type: ignore[call-arg]


def test_safe_summary_excludes_secrets() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    summary = settings.safe_summary()
    dumped = str(summary)
    assert "local-dev-token" not in dumped
    assert "repopilot-secret" not in dumped
    assert "api_token" not in summary
    assert "minio_secret_key" not in summary


def test_pypi_proxy_must_use_the_dedicated_internal_endpoint() -> None:
    with pytest.raises(ValueError, match="pypi-proxy"):
        Settings(_env_file=None, pypi_proxy_url="http://example.com:3128")  # type: ignore[call-arg]


def test_pypi_network_must_use_the_dedicated_internal_network() -> None:
    with pytest.raises(ValueError, match="repopilot-pypi-egress"):
        Settings(_env_file=None, pypi_egress_network="bridge")  # type: ignore[call-arg]

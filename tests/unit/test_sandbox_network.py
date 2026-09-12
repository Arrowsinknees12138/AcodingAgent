from repopilot.infrastructure.sandbox.docker import _sandbox_network_settings


def test_runtime_sandbox_has_no_network_or_proxy_environment() -> None:
    network, environment = _sandbox_network_settings(
        network_enabled=False,
        proxy_url="http://pypi-proxy:3128",
        egress_network="repopilot-pypi-egress",
    )

    assert network == "none"
    assert environment == {}


def test_build_sandbox_uses_only_internal_pypi_proxy_network() -> None:
    network, environment = _sandbox_network_settings(
        network_enabled=True,
        proxy_url="http://pypi-proxy:3128",
        egress_network="repopilot-pypi-egress",
    )

    assert network == "repopilot-pypi-egress"
    assert environment["HTTPS_PROXY"] == "http://pypi-proxy:3128"
    assert environment["PIP_INDEX_URL"] == "https://pypi.org/simple"
    assert environment["NO_PROXY"] == ""

"""依赖安装只能从官方 PyPI 解析，并优先尊重 lockfile。"""

from __future__ import annotations

import io
import tarfile

import pytest

from repopilot.services.dependency_service import (
    DependencyManifestError,
    plan_dependency_install,
)


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_uv_lock_is_read_and_installed_frozen_from_official_pypi() -> None:
    plan = plan_dependency_install(
        _archive(
            {
                "pyproject.toml": b'[project]\nname="demo"\ndependencies=["httpx"]\n',
                "uv.lock": (
                    b'version = 1\n[[package]]\nname = "httpx"\nversion = "1.0"\n'
                    b'source = { registry = "https://pypi.org/simple" }\n'
                ),
            }
        )
    )
    assert plan.manager == "uv"
    assert plan.frozen is True
    assert plan.command is not None
    assert "--frozen" in plan.command.args
    assert plan.allowed_hosts == ("pypi.org", "files.pythonhosted.org")
    assert plan.cache_enabled is True


def test_requirements_rejects_index_override_and_direct_url() -> None:
    for line in (
        b"--extra-index-url https://evil.invalid/simple\nhttpx==1.0\n",
        b"demo @ https://evil.invalid/demo.whl\n",
        b"-e git+https://evil.invalid/repo.git\n",
    ):
        with pytest.raises(DependencyManifestError):
            plan_dependency_install(_archive({"requirements.txt": line}))


def test_uv_lock_rejects_non_official_registry() -> None:
    with pytest.raises(DependencyManifestError, match="非官方 registry"):
        plan_dependency_install(
            _archive(
                {
                    "uv.lock": (
                        b'version = 1\n[[package]]\nname = "demo"\nversion = "1"\n'
                        b'source = { registry = "https://mirror.invalid/simple" }\n'
                    )
                }
            )
        )


def test_pyproject_direct_reference_is_rejected() -> None:
    with pytest.raises(DependencyManifestError, match="直接 URL"):
        plan_dependency_install(
            _archive(
                {
                    "pyproject.toml": (
                        b'[project]\nname="demo"\n'
                        b'dependencies=["pkg @ https://evil.invalid/pkg.whl"]\n'
                    )
                }
            )
        )


def test_repository_without_dependencies_needs_no_install() -> None:
    plan = plan_dependency_install(
        _archive({"pyproject.toml": b'[project]\nname="demo"\ndependencies=[]\n'})
    )
    assert plan.manager == "none"
    assert plan.command is None

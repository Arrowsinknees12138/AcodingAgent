"""Python 依赖清单检查与 Build Sandbox 安装计划。"""

from __future__ import annotations

import io
import re
import tarfile
import tomllib
from typing import Literal

from repopilot.domain import StrictModel
from repopilot.domain.policies import OFFICIAL_PYPI_INDEX, DependencyPolicy

OfficialPypiHost = Literal["pypi.org", "files.pythonhosted.org"]
OFFICIAL_PYPI_HOSTS: tuple[OfficialPypiHost, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
)
_REMOTE_REFERENCE_RE = re.compile(r"(?:@\s*)?(?:https?|git\+|ssh:|git@)", re.IGNORECASE)


class DependencyManifestError(ValueError):
    pass


class DependencyInstallCommand(StrictModel):
    executable: Literal["python", "uv"]
    args: tuple[str, ...]


class DependencyInstallPlan(StrictModel):
    manager: Literal["none", "uv", "pip"]
    manifest_path: str | None
    command: DependencyInstallCommand | None
    frozen: bool
    index_url: Literal["https://pypi.org/simple"] = OFFICIAL_PYPI_INDEX
    allowed_hosts: tuple[OfficialPypiHost, ...] = OFFICIAL_PYPI_HOSTS
    cache_enabled: bool = True


def plan_dependency_install(
    source_archive: bytes,
    policy: DependencyPolicy | None = None,
) -> DependencyInstallPlan:
    policy = policy or DependencyPolicy()
    manifests = _read_manifests(source_archive)
    pyproject = _parse_toml(manifests.get("pyproject.toml"), "pyproject.toml")

    uv_lock = manifests.get("uv.lock")
    if uv_lock is not None:
        _validate_uv_lock(uv_lock)
        _validate_pyproject_sources(pyproject)
        return DependencyInstallPlan(
            manager="uv",
            manifest_path="uv.lock",
            command=DependencyInstallCommand(
                executable="uv",
                args=("sync", "--frozen", "--no-dev", "--index-url", policy.index_url),
            ),
            frozen=True,
            cache_enabled=policy.allow_cache,
        )

    requirements_path = next(
        (
            path
            for path in sorted(manifests)
            if path.rsplit("/", 1)[-1].lower() == "requirements.txt"
        ),
        None,
    )
    if requirements_path is not None:
        _validate_requirements(manifests[requirements_path], requirements_path)
        return DependencyInstallPlan(
            manager="pip",
            manifest_path=requirements_path,
            command=DependencyInstallCommand(
                executable="python",
                args=(
                    "-m",
                    "pip",
                    "install",
                    "--only-binary=:all:",
                    "--index-url",
                    policy.index_url,
                    "--requirement",
                    requirements_path,
                ),
            ),
            frozen=_requirements_are_hashed(manifests[requirements_path]),
            cache_enabled=policy.allow_cache,
        )

    dependencies = _project_dependencies(pyproject)
    if dependencies:
        _validate_pep508_dependencies(dependencies)
        _validate_pyproject_sources(pyproject)
        return DependencyInstallPlan(
            manager="pip",
            manifest_path="pyproject.toml",
            command=DependencyInstallCommand(
                executable="python",
                args=(
                    "-m",
                    "pip",
                    "install",
                    "--only-binary=:all:",
                    "--index-url",
                    policy.index_url,
                    ".",
                ),
            ),
            frozen=False,
            cache_enabled=policy.allow_cache,
        )

    return DependencyInstallPlan(
        manager="none",
        manifest_path=None,
        command=None,
        frozen=True,
        cache_enabled=policy.allow_cache,
    )


def _read_manifests(source_archive: bytes) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(source_archive), mode="r:gz") as archive:
            for member in archive.getmembers():
                path = member.name.replace("\\", "/").removeprefix("./")
                name = path.rsplit("/", 1)[-1].lower()
                wanted = name in {"pyproject.toml", "uv.lock", "requirements.txt"}
                if not member.isfile() or not wanted:
                    continue
                if member.size > 4 * 1024 * 1024:
                    raise DependencyManifestError(f"依赖清单过大: {path}")
                stream = archive.extractfile(member)
                if stream is not None:
                    result[path] = stream.read(4 * 1024 * 1024 + 1)
    except tarfile.TarError as exc:
        raise DependencyManifestError("Source Archive 不是合法 tar.gz") from exc
    return result


def _parse_toml(content: bytes | None, name: str) -> dict[str, object]:
    if content is None:
        return {}
    try:
        return tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise DependencyManifestError(f"{name} 无法解析") from exc


def _validate_uv_lock(content: bytes) -> None:
    lock = _parse_toml(content, "uv.lock")
    packages = lock.get("package", [])
    if not isinstance(packages, list):
        raise DependencyManifestError("uv.lock package 必须是数组")
    for package in packages:
        if not isinstance(package, dict):
            raise DependencyManifestError("uv.lock package 条目格式错误")
        source = package.get("source")
        if not isinstance(source, dict):
            continue
        if any(key in source for key in ("git", "url")):
            raise DependencyManifestError("uv.lock 禁止 Git 或 URL 依赖源")
        registry = source.get("registry")
        if isinstance(registry, str) and registry.rstrip("/") != OFFICIAL_PYPI_INDEX:
            raise DependencyManifestError(f"uv.lock 包含非官方 registry: {registry}")


def _validate_requirements(content: bytes, path: str) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DependencyManifestError(f"{path} 不是 UTF-8") from exc
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("--hash="):
            continue
        if line.startswith("-") or _REMOTE_REFERENCE_RE.search(line):
            raise DependencyManifestError(f"{path}:{number} 包含非 PyPI 依赖源或 pip 选项")


def _requirements_are_hashed(content: bytes) -> bool:
    text = content.decode("utf-8", errors="replace")
    requirements = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "--hash="))
    ]
    return bool(requirements) and all("==" in line for line in requirements) and "--hash=" in text


def _project_dependencies(pyproject: dict[str, object]) -> tuple[str, ...]:
    project = pyproject.get("project")
    if not isinstance(project, dict):
        return ()
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(value, str) for value in dependencies
    ):
        raise DependencyManifestError("project.dependencies 必须是字符串数组")
    return tuple(dependencies)


def _validate_pep508_dependencies(dependencies: tuple[str, ...]) -> None:
    for dependency in dependencies:
        if _REMOTE_REFERENCE_RE.search(dependency):
            raise DependencyManifestError(f"禁止直接 URL/Git 依赖: {dependency}")


def _validate_pyproject_sources(pyproject: dict[str, object]) -> None:
    tool = pyproject.get("tool")
    if not isinstance(tool, dict):
        return
    uv = tool.get("uv")
    if isinstance(uv, dict) and uv.get("sources"):
        raise DependencyManifestError("Phase 0 禁止 tool.uv.sources 自定义依赖源")

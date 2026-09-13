"""Isolated repository-snapshot workspace for the interactive coding agent."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Mapping
from typing import Literal
from uuid import UUID

from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.plans import DeveloperFileEdit
from repopilot.services.artifact_store import ArtifactStore
from repopilot.services.sandbox_service import RunCommandRequest, SandboxService, SandboxSpec
from repopilot.tools.paths import normalize_relative_path, require_allowed_path
from repopilot.tools.registry import ToolContext
from repopilot.tools.workspace_models import (
    DeleteFileRequest,
    DeleteFileResult,
    ReadFileRequest,
    ReadFileResult,
    SearchCodeRequest,
    SearchCodeResult,
    SearchMatch,
    WorkspaceCommandResult,
    WriteFileRequest,
    WriteFileResult,
)

_MAX_ARCHIVE_FILES = 20_000
_MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
_MAX_READ_CHARS = 32_000
_MAX_SEARCH_FILE_BYTES = 1024 * 1024
_MAX_COMMAND_OUTPUT_CHARS = 16_000


class ArchiveWorkspaceBackend:
    """Read the tracked snapshot, stage exact-path edits, and test in a fresh sandbox."""

    def __init__(
        self,
        *,
        archive: bytes,
        run_id: UUID,
        tenant_id: UUID,
        base_revision: str,
        artifact_store: ArtifactStore,
        sandbox: SandboxService,
        dependency_layer_key: str | None,
    ) -> None:
        self._original = _load_regular_files(archive)
        self._current = self._original.copy()
        self._run_id = run_id
        self._tenant_id = tenant_id
        self._base_revision = base_revision
        self._artifacts = artifact_store
        self._sandbox = sandbox
        self._dependency_layer_key = dependency_layer_key

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(sorted(self._current))

    async def read_file(self, request: ReadFileRequest, context: ToolContext) -> ReadFileResult:
        path = normalize_relative_path(request.path)
        if not context.read_all_repository_files:
            require_allowed_path(path, (*context.read_paths, *context.write_paths))
        raw = self._current.get(path)
        if raw is None:
            raise ValueError(f"repository file does not exist: {path}")
        try:
            lines = raw.decode("utf-8").splitlines(keepends=True)
        except UnicodeDecodeError as exc:
            raise ValueError(f"repository file is not UTF-8 text: {path}") from exc
        end = request.end_line or min(len(lines), request.start_line + 199)
        if end < request.start_line:
            raise ValueError("end_line must not precede start_line")
        content = "".join(lines[request.start_line - 1 : end])
        truncated = len(content) > _MAX_READ_CHARS or end < len(lines)
        return ReadFileResult(path=path, content=content[:_MAX_READ_CHARS], truncated=truncated)

    async def search_code(
        self, request: SearchCodeRequest, context: ToolContext
    ) -> SearchCodeResult:
        requested = tuple(normalize_relative_path(path) for path in request.paths)
        if not context.read_all_repository_files:
            for path in requested:
                require_allowed_path(path, context.read_paths)
        paths = requested or (
            self.paths if context.read_all_repository_files else context.read_paths
        )
        matches: list[SearchMatch] = []
        truncated = False
        for path in paths:
            raw = self._current.get(path)
            if raw is None or len(raw) > _MAX_SEARCH_FILE_BYTES:
                continue
            try:
                source = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            for number, line in enumerate(source.splitlines(), start=1):
                if request.query.casefold() not in line.casefold():
                    continue
                if len(matches) >= request.max_results:
                    truncated = True
                    break
                matches.append(SearchMatch(path=path, line=number, text=line[:500]))
            if truncated:
                break
        return SearchCodeResult(matches=tuple(matches), truncated=truncated)

    async def write_file(self, request: WriteFileRequest, context: ToolContext) -> WriteFileResult:
        path = require_allowed_path(request.path, context.write_paths)
        content = request.content.encode("utf-8")
        self._current[path] = content
        return WriteFileResult(path=path, size_bytes=len(content))

    async def delete_file(
        self, request: DeleteFileRequest, context: ToolContext
    ) -> DeleteFileResult:
        path = require_allowed_path(request.path, context.write_paths)
        if path not in self._current:
            raise ValueError(f"repository file does not exist: {path}")
        del self._current[path]
        return DeleteFileResult(path=path, deleted=True)

    async def run_command(
        self, request: RunCommandRequest, context: ToolContext
    ) -> WorkspaceCommandResult:
        if not context.command_sandbox_on_demand:
            raise PermissionError("isolated command sandbox is not enabled")
        archive_ref = await self._artifacts.put_bytes(
            ArtifactKind.SOURCE_ARCHIVE,
            _pack_regular_files(self._current),
            ArtifactMetadata(
                tenant_id=self._tenant_id,
                run_id=self._run_id,
                base_revision=self._base_revision,
                schema_version="1",
            ),
        )
        sandbox_id = await self._sandbox.create(
            SandboxSpec(
                run_id=self._run_id,
                work_item_id=context.work_item_id,
                image="python:3.12-slim",
                source_archive_ref=archive_ref,
                test_bundle_ref=None,
                dependency_layer_key=self._dependency_layer_key,
                network_enabled=False,
                wall_time_seconds=request.timeout_seconds,
            )
        )
        try:
            cwd = "/workspace" if request.cwd == "." else f"/workspace/{request.cwd}"
            result = await self._sandbox.execute(
                sandbox_id, request.model_copy(update={"cwd": cwd})
            )
            caller = ArtifactCaller(
                tenant_id=self._tenant_id,
                run_id=self._run_id,
                role=None,
                service="developer-command-result",
            )
            stdout = (await self._artifacts.get_bytes(result.stdout_ref, caller)).decode(
                "utf-8", errors="replace"
            )
            stderr = (await self._artifacts.get_bytes(result.stderr_ref, caller)).decode(
                "utf-8", errors="replace"
            )
        finally:
            await self._sandbox.destroy(sandbox_id)
        return WorkspaceCommandResult(
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            oom_killed=result.oom_killed,
            duration_ms=result.duration_ms,
            stdout=stdout[:_MAX_COMMAND_OUTPUT_CHARS],
            stderr=stderr[:_MAX_COMMAND_OUTPUT_CHARS],
            truncated=(
                len(stdout) > _MAX_COMMAND_OUTPUT_CHARS or len(stderr) > _MAX_COMMAND_OUTPUT_CHARS
            ),
        )

    def final_edits(self, allowed_write_paths: tuple[str, ...]) -> tuple[DeveloperFileEdit, ...]:
        allowed = {normalize_relative_path(path) for path in allowed_write_paths}
        changed = {
            path
            for path in self._original.keys() | self._current.keys()
            if self._original.get(path) != self._current.get(path)
        }
        denied = changed - allowed
        if denied:
            raise PermissionError(f"agent modified unapproved paths: {', '.join(sorted(denied))}")
        edits: list[DeveloperFileEdit] = []
        for path in sorted(changed):
            current = self._current.get(path)
            operation: Literal["create", "modify", "delete"]
            if current is None:
                operation = "delete"
                content = None
            else:
                operation = "modify" if path in self._original else "create"
                content = current.decode("utf-8")
            edits.append(DeveloperFileEdit(path=path, operation=operation, content=content))
        return tuple(edits)

    def original_text(self, paths: tuple[str, ...]) -> dict[str, str | None]:
        return {
            path: self._original[path].decode("utf-8") if path in self._original else None
            for path in paths
        }


def _load_regular_files(archive: bytes) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    size = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for member in source:
            if not member.isfile():
                continue
            path = normalize_relative_path(member.name.removeprefix("./"))
            if path.startswith((".repopilot/", ".git/")):
                continue
            if path in files or len(files) >= _MAX_ARCHIVE_FILES:
                raise ValueError("repository snapshot has duplicate paths or too many files")
            size += member.size
            if size > _MAX_ARCHIVE_BYTES:
                raise ValueError("repository snapshot exceeds workspace size limit")
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError(f"cannot read repository file: {path}")
            files[path] = stream.read()
    return files


def _pack_regular_files(files: Mapping[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, content in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(content)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(content))
    return output.getvalue()

from __future__ import annotations

import io
import tarfile
from uuid import UUID, uuid4

import pytest

from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.enums import AgentRole, ArtifactKind
from repopilot.services.sandbox_service import CommandResult, RunCommandRequest, SandboxSpec
from repopilot.tools.archive_workspace import ArchiveWorkspaceBackend
from repopilot.tools.delete_file import DeleteFileTool
from repopilot.tools.read_file import ReadFileTool
from repopilot.tools.registry import ToolContext, ToolNotAllowedError, ToolRegistry
from repopilot.tools.run_command import RunCommandTool
from repopilot.tools.search_code import SearchCodeTool
from repopilot.tools.workspace_models import ReadFileRequest, SearchCodeRequest
from repopilot.tools.write_file import WriteFileTool
from tests.fakes import MemoryArtifactStore


def _archive(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for path, content in files.items():
            member = tarfile.TarInfo(path)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return output.getvalue()


class FakeCommandSandbox:
    def __init__(self, store: MemoryArtifactStore, metadata: ArtifactMetadata) -> None:
        self.store = store
        self.metadata = metadata
        self.last_spec: SandboxSpec | None = None
        self.destroyed = False

    async def create(self, spec: SandboxSpec) -> UUID:
        self.last_spec = spec
        return uuid4()

    async def execute(self, sandbox_id: UUID, request: RunCommandRequest) -> CommandResult:
        del sandbox_id
        assert self.last_spec is not None
        assert self.last_spec.network_enabled is False
        assert request.cwd == "/workspace"
        source = self.store.content[self.last_spec.source_archive_ref.artifact_id]
        with tarfile.open(fileobj=io.BytesIO(source), mode="r:gz") as archive:
            content = archive.extractfile("src/app.py")
            assert content is not None
            assert b"return 2" in content.read()
        stdout = await self.store.put_bytes(ArtifactKind.LOG, b"1 passed", self.metadata)
        stderr = await self.store.put_bytes(ArtifactKind.LOG, b"", self.metadata)
        return CommandResult(
            exit_code=0,
            timed_out=False,
            oom_killed=False,
            duration_ms=100,
            stdout_ref=stdout,
            stderr_ref=stderr,
        )

    async def destroy(self, sandbox_id: UUID) -> None:
        del sandbox_id
        self.destroyed = True


async def test_archive_workspace_reads_entire_snapshot_but_writes_only_approved_paths() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id, item_id = uuid4(), uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id, run_id=run_id, base_revision="a" * 40, schema_version="1"
    )
    sandbox = FakeCommandSandbox(store, metadata)
    backend = ArchiveWorkspaceBackend(
        archive=_archive(
            {
                "src/app.py": b"def value():\n    return 1\n",
                "tests/test_base.py": b"def test_value(): pass\n",
                ".repopilot/sealed_tests/secret.py": b"SECRET = True\n",
            }
        ),
        run_id=run_id,
        tenant_id=tenant_id,
        base_revision="a" * 40,
        artifact_store=store,
        sandbox=sandbox,  # type: ignore[arg-type]
        dependency_layer_key=None,
    )
    context = ToolContext(
        tenant_id=tenant_id,
        run_id=run_id,
        work_item_id=item_id,
        sandbox_id=None,
        read_paths=("src/app.py",),
        write_paths=("src/app.py",),
        read_all_repository_files=True,
        command_sandbox_on_demand=True,
    )
    registry = ToolRegistry(
        (
            ReadFileTool(backend),
            SearchCodeTool(backend),
            WriteFileTool(backend),
            DeleteFileTool(backend),
            RunCommandTool(backend),
        )
    )
    readable = await registry.execute(
        role=AgentRole.DEVELOPER,
        allowed_tools=("read_file", "search_code", "write_file", "delete_file", "run_command"),
        name="read_file",
        arguments=ReadFileRequest(path="tests/test_base.py").model_dump(),
        context=context,
    )
    assert "test_value" in readable.content  # type: ignore[attr-defined]
    search = await backend.search_code(SearchCodeRequest(query="test_value"), context)
    assert search.matches[0].path == "tests/test_base.py"
    assert ".repopilot/sealed_tests/secret.py" not in backend.paths
    with pytest.raises(ToolNotAllowedError):
        await registry.execute(
            role=AgentRole.DEVELOPER,
            allowed_tools=("write_file",),
            name="write_file",
            arguments={"path": "tests/test_base.py", "content": "modified"},
            context=context,
        )
    await registry.execute(
        role=AgentRole.DEVELOPER,
        allowed_tools=("write_file",),
        name="write_file",
        arguments={"path": "src/app.py", "content": "def value():\n    return 2\n"},
        context=context,
    )
    result = await registry.execute(
        role=AgentRole.DEVELOPER,
        allowed_tools=("run_command",),
        name="run_command",
        arguments={"executable": "pytest", "args": ["-q"], "cwd": ".", "timeout_seconds": 60},
        context=context,
    )
    assert result.stdout == "1 passed"  # type: ignore[attr-defined]
    assert sandbox.destroyed
    assert backend.final_edits(("src/app.py",))[0].content == "def value():\n    return 2\n"

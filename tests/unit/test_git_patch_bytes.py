"""The repository integrator must not rewrite model-generated patch bytes."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.plans import PatchProposal
from repopilot.infrastructure.git.repository_service import GitRepositoryService
from tests.fakes import MemoryArtifactStore


async def test_integrate_patch_preserves_lf_bytes_in_staging_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    base_revision = "a" * 40
    store = MemoryArtifactStore()
    service = GitRepositoryService(
        data_dir=tmp_path / "data", artifact_store=store, tenant_id=tenant_id
    )
    (service._integration_dir / str(run_id)).mkdir()

    patch_bytes = (
        b"diff --git a/calculator.py b/calculator.py\n"
        b"--- a/calculator.py\n"
        b"+++ b/calculator.py\n"
        b"@@ -1 +1 @@\n"
        b"-return a - b\n"
        b"+return a + b\n"
    )
    metadata = ArtifactMetadata(
        tenant_id=tenant_id, run_id=run_id, base_revision=base_revision, schema_version="1"
    )
    patch_ref = await store.put_bytes(ArtifactKind.PATCH, patch_bytes, metadata)
    proposal = PatchProposal(
        work_item_id=uuid4(),
        attempt=1,
        input_revision=base_revision,
        patch_ref=patch_ref,
        touched_paths=("calculator.py",),
    )
    proposal_ref = await store.put_bytes(
        ArtifactKind.PATCH, proposal.model_dump_json().encode(), metadata
    )
    staged_bytes: list[bytes] = []

    async def fake_git_run(args: list[str], *, cwd: Path, **kwargs: object) -> str:
        del cwd, kwargs
        if args[:2] == ["worktree", "add"]:
            return ""
        if args[:2] == ["apply", "--check"]:
            staged_bytes.append(Path(args[2]).read_bytes())
            raise RuntimeError("stop after inspecting patch bytes")
        raise AssertionError(f"unexpected git command: {args}")

    async def fake_remove_worktree(root: Path, staging: Path) -> None:
        del root, staging

    monkeypatch.setattr(service._git, "run", fake_git_run)
    monkeypatch.setattr(service, "_remove_worktree", fake_remove_worktree)

    with pytest.raises(RuntimeError, match="stop after inspecting patch bytes"):
        await service.integrate_patch(proposal_ref, allowed_write_paths=("calculator.py",))

    assert staged_bytes == [patch_bytes]


async def test_integrate_lf_patch_against_lf_repository_on_windows(tmp_path: Path) -> None:
    tenant_id, run_id = uuid4(), uuid4()
    store = MemoryArtifactStore()
    service = GitRepositoryService(
        data_dir=tmp_path / "data", artifact_store=store, tenant_id=tenant_id
    )
    origin = tmp_path / "origin"
    origin.mkdir()
    await service._git.run(["init", "-b", "main"], cwd=origin)
    (origin / "calculator.py").write_bytes(
        b'"""Small target module for a RepoPilot repair test."""\n'
        b"\n\n"
        b"def add(a: int, b: int) -> int:\n"
        b'    """Return the sum of two integers."""\n'
        b"    return a - b  # Intentionally wrong: the agent should repair this line.\n"
        b"\n\n"
        b"def subtract(a: int, b: int) -> int:\n"
        b'    """Return the difference of two integers."""\n'
        b"    return a - b\n"
    )
    await service._git.run(["add", "calculator.py"], cwd=origin)
    await service._git.run(
        ["commit", "-m", "initial"],
        cwd=origin,
        env_overrides={
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    )
    base_revision = (await service._git.run(["rev-parse", "HEAD"], cwd=origin)).strip()
    integration = service._integration_dir / str(run_id)
    await service._git.run(
        ["worktree", "add", "--detach", str(integration), base_revision], cwd=origin
    )

    patch_bytes = (
        b"diff --git a/calculator.py b/calculator.py\n"
        b"--- a/calculator.py\n"
        b"+++ b/calculator.py\n"
        b"@@ -3,7 +3,7 @@\n"
        b" \n"
        b" def add(a: int, b: int) -> int:\n"
        b'     """Return the sum of two integers."""\n'
        b"-    return a - b  # Intentionally wrong: the agent should repair this line.\n"
        b"+    return a + b\n"
        b" \n"
        b" \n"
        b" def subtract(a: int, b: int) -> int:\n"
    )
    metadata = ArtifactMetadata(
        tenant_id=tenant_id, run_id=run_id, base_revision=base_revision, schema_version="1"
    )
    patch_ref = await store.put_bytes(ArtifactKind.PATCH, patch_bytes, metadata)
    proposal = PatchProposal(
        work_item_id=uuid4(),
        attempt=1,
        input_revision=base_revision,
        patch_ref=patch_ref,
        touched_paths=("calculator.py",),
    )
    proposal_ref = await store.put_bytes(
        ArtifactKind.PATCH, proposal.model_dump_json().encode(), metadata
    )

    integrated_ref = await service.integrate_patch(
        proposal_ref, allowed_write_paths=("calculator.py",)
    )

    assert integrated_ref.kind is ArtifactKind.INTEGRATED_PATCH
    assert (integration / "calculator.py").read_text(encoding="utf-8").count("return a + b") == 1

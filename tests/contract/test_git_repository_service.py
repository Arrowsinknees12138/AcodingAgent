"""`GitRepositoryService` 的契约测试：真实 git 仓库 + 真实 MinIO/PostgreSQL。

覆盖实施设计 Milestone 4 验收标准："上游接口变化能进入下游
DeveloperContext，越权 patch 被拒绝"。
"""

from __future__ import annotations

import tarfile
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from repopilot.domain.artifacts import (
    ArtifactCaller,
    ArtifactMetadata,
    ExportedInterfacePayload,
    RepositorySnapshot,
)
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.plans import DeveloperContext, IntegratedPatch, PatchProposal, WorkItem
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.infrastructure.artifacts.minio import MinioArtifactStore
from repopilot.infrastructure.git.repository_service import GitRepositoryService
from repopilot.services.repository_service import (
    DependencyChangeDeniedError,
    PatchApplyFailedError,
    PatchConflictError,
    PatchPathDeniedError,
)
from tests.contract.conftest_git import init_origin_repo, make_patch

pytestmark = pytest.mark.contract

TENANT_ID = uuid4()


def _make_task_spec(*, run_id: UUID, repository_url: str, base_revision: str) -> TaskSpec:
    return TaskSpec(
        task_id=uuid4(),
        run_id=run_id,
        tenant_id=TENANT_ID,
        repository_url=repository_url,
        base_revision=base_revision,
        requirement="demo requirement",
        acceptance_criteria=("must pass",),
        acceptance_criteria_source="structured",
        policy_profile="default",
        budget=BudgetInput(
            max_cost_usd=Decimal("5"),
            max_wall_time_seconds=600,
            max_model_calls=10,
            max_sandbox_seconds=300,
        ),
    )


def _trusted_caller(run_id: UUID) -> ArtifactCaller:
    return ArtifactCaller(tenant_id=TENANT_ID, run_id=run_id, role=None, service="repository")


async def _store_patch_proposal(
    artifact_store: MinioArtifactStore,
    run_id: UUID,
    input_revision: str,
    patch_bytes: bytes,
    touched_paths: tuple[str, ...],
):
    metadata = ArtifactMetadata(
        tenant_id=TENANT_ID, run_id=run_id, base_revision=input_revision, schema_version="1"
    )
    patch_ref = await artifact_store.put_bytes(ArtifactKind.PATCH, patch_bytes, metadata)
    proposal = PatchProposal(
        work_item_id=uuid4(),
        attempt=1,
        input_revision=input_revision,
        patch_ref=patch_ref,
        touched_paths=touched_paths,
    )
    return await artifact_store.put_bytes(
        ArtifactKind.PATCH, proposal.model_dump_json().encode("utf-8"), metadata
    )


@pytest.fixture
def origin_repo(tmp_path: Path) -> tuple[Path, str]:
    origin_path = tmp_path / "origin"
    base_revision = init_origin_repo(origin_path)
    return origin_path, base_revision


@pytest.fixture
def repository_service(tmp_path: Path, artifact_store: MinioArtifactStore) -> GitRepositoryService:
    data_dir = tmp_path / "repopilot-data"
    return GitRepositoryService(
        data_dir=data_dir, artifact_store=artifact_store, tenant_id=TENANT_ID
    )


async def test_resolve_revision_returns_full_sha(
    repository_service: GitRepositoryService, origin_repo: tuple[Path, str]
) -> None:
    origin_path, base_revision = origin_repo
    resolved = await repository_service.resolve_revision(str(origin_path), None)
    assert resolved == base_revision


async def test_snapshot_produces_source_archive_and_symbol_index(
    repository_service: GitRepositoryService,
    origin_repo: tuple[Path, str],
    artifact_store: MinioArtifactStore,
) -> None:
    origin_path, base_revision = origin_repo
    run_id = uuid4()
    task = _make_task_spec(
        run_id=run_id, repository_url=str(origin_path), base_revision=base_revision
    )
    snapshot_ref = await repository_service.snapshot(task)

    caller = _trusted_caller(run_id)
    snapshot = RepositorySnapshot.model_validate_json(
        await artifact_store.get_bytes(snapshot_ref, caller)
    )
    assert snapshot.primary_language == "python"
    assert "pyproject.toml" in snapshot.dependency_manifest_paths

    symbol_index_bytes = await artifact_store.get_bytes(snapshot.symbol_index_ref, caller)
    assert b"foo" in symbol_index_bytes
    assert b"bar" in symbol_index_bytes

    source_archive_bytes = await artifact_store.get_bytes(snapshot.source_archive_ref, caller)
    with tarfile.open(fileobj=BytesIO(source_archive_bytes), mode="r:gz") as tar:
        names = tar.getnames()
    assert "pkg/foo.py" in names
    assert not any(name == ".git" or name.startswith(".git/") for name in names)


async def test_integrate_patch_rejects_path_outside_allowlist(
    repository_service: GitRepositoryService,
    origin_repo: tuple[Path, str],
    artifact_store: MinioArtifactStore,
) -> None:
    origin_path, base_revision = origin_repo
    run_id = uuid4()
    task = _make_task_spec(
        run_id=run_id, repository_url=str(origin_path), base_revision=base_revision
    )
    await repository_service.snapshot(task)

    patch_bytes, touched_paths = make_patch(
        origin_path, base_revision, {"pkg/bar.py": "def bar():\n    return 999\n"}
    )
    proposal_ref = await _store_patch_proposal(
        artifact_store, run_id, base_revision, patch_bytes, touched_paths
    )

    with pytest.raises(PatchPathDeniedError):
        await repository_service.integrate_patch(
            proposal_ref,
            allowed_write_paths=("pkg/foo.py",),  # bar.py 不在允许列表里
        )


async def test_integrate_patch_requires_explicit_dependency_change_authorization(
    repository_service: GitRepositoryService,
    origin_repo: tuple[Path, str],
    artifact_store: MinioArtifactStore,
) -> None:
    origin_path, base_revision = origin_repo
    run_id = uuid4()
    task = _make_task_spec(
        run_id=run_id, repository_url=str(origin_path), base_revision=base_revision
    )
    await repository_service.snapshot(task)
    patch_bytes, touched_paths = make_patch(
        origin_path,
        base_revision,
        {"pyproject.toml": '[project]\nname = "demo"\ndependencies = ["httpx"]\n'},
    )
    proposal_ref = await _store_patch_proposal(
        artifact_store, run_id, base_revision, patch_bytes, touched_paths
    )

    with pytest.raises(DependencyChangeDeniedError):
        await repository_service.integrate_patch(
            proposal_ref,
            allowed_write_paths=("pyproject.toml",),
        )

    integrated_ref = await repository_service.integrate_patch(
        proposal_ref,
        allowed_write_paths=("pyproject.toml",),
        allow_dependency_changes=True,
    )
    assert integrated_ref.kind is ArtifactKind.INTEGRATED_PATCH


async def test_integrate_patch_applies_and_downstream_context_sees_new_interface(
    repository_service: GitRepositoryService,
    origin_repo: tuple[Path, str],
    artifact_store: MinioArtifactStore,
) -> None:
    origin_path, base_revision = origin_repo
    run_id = uuid4()
    task = _make_task_spec(
        run_id=run_id, repository_url=str(origin_path), base_revision=base_revision
    )
    await repository_service.snapshot(task)

    patch_bytes, touched_paths = make_patch(
        origin_path,
        base_revision,
        {"pkg/foo.py": "def foo():\n    return 1\n\n\ndef new_upstream_helper():\n    return 42\n"},
    )
    proposal_ref = await _store_patch_proposal(
        artifact_store, run_id, base_revision, patch_bytes, touched_paths
    )

    integrated_ref = await repository_service.integrate_patch(
        proposal_ref, allowed_write_paths=("pkg/foo.py",)
    )

    caller = _trusted_caller(run_id)
    integrated = IntegratedPatch.model_validate_json(
        await artifact_store.get_bytes(integrated_ref, caller)
    )
    interface = ExportedInterfacePayload.model_validate_json(
        await artifact_store.get_bytes(integrated.exported_interface_ref, caller)
    )
    added_names = {s.qualified_name for s in interface.symbols if s.change == "added"}
    assert "new_upstream_helper" in added_names

    downstream_work_item = WorkItem(
        work_item_id=uuid4(),
        run_id=run_id,
        kind="code",
        dependencies=(),
        allowed_write_paths=("pkg/bar.py",),
        read_paths=("pkg/foo.py", "pkg/bar.py"),
        owner="developer-1",
        attempt=1,
    )
    context_ref = await repository_service.create_developer_context(
        downstream_work_item, (integrated_ref,)
    )

    context = DeveloperContext.model_validate_json(
        await artifact_store.get_bytes(context_ref, caller)
    )
    assert context.upstream_interface_refs == (integrated.exported_interface_ref,)

    source_bytes = await artifact_store.get_bytes(context.source_archive_ref, caller)
    with tarfile.open(fileobj=BytesIO(source_bytes), mode="r:gz") as tar:
        foo_member = tar.extractfile("pkg/foo.py")
        assert foo_member is not None
        assert b"new_upstream_helper" in foo_member.read()


async def test_conflicting_patches_on_same_input_revision_raise_conflict(
    repository_service: GitRepositoryService,
    origin_repo: tuple[Path, str],
    artifact_store: MinioArtifactStore,
) -> None:
    origin_path, base_revision = origin_repo
    run_id = uuid4()
    task = _make_task_spec(
        run_id=run_id, repository_url=str(origin_path), base_revision=base_revision
    )
    await repository_service.snapshot(task)

    first_patch_bytes, first_touched = make_patch(
        origin_path, base_revision, {"pkg/foo.py": "def foo():\n    return 100\n"}
    )
    second_patch_bytes, second_touched = make_patch(
        origin_path, base_revision, {"pkg/foo.py": "def foo():\n    return 200\n"}
    )

    first_proposal_ref = await _store_patch_proposal(
        artifact_store, run_id, base_revision, first_patch_bytes, first_touched
    )
    await repository_service.integrate_patch(
        first_proposal_ref, allowed_write_paths=("pkg/foo.py",)
    )

    second_proposal_ref = await _store_patch_proposal(
        artifact_store, run_id, base_revision, second_patch_bytes, second_touched
    )
    with pytest.raises(PatchConflictError):
        await repository_service.integrate_patch(
            second_proposal_ref, allowed_write_paths=("pkg/foo.py",)
        )


async def test_integrate_patch_rejects_syntax_error(
    repository_service: GitRepositoryService,
    origin_repo: tuple[Path, str],
    artifact_store: MinioArtifactStore,
) -> None:
    origin_path, base_revision = origin_repo
    run_id = uuid4()
    task = _make_task_spec(
        run_id=run_id, repository_url=str(origin_path), base_revision=base_revision
    )
    await repository_service.snapshot(task)

    patch_bytes, touched_paths = make_patch(
        origin_path, base_revision, {"pkg/foo.py": "def foo(:\n    return 1\n"}
    )
    proposal_ref = await _store_patch_proposal(
        artifact_store, run_id, base_revision, patch_bytes, touched_paths
    )

    with pytest.raises(PatchApplyFailedError):
        await repository_service.integrate_patch(proposal_ref, allowed_write_paths=("pkg/foo.py",))

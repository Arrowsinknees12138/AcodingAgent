from __future__ import annotations

import io
import json
import tarfile
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from temporalio.exceptions import ApplicationError

from repopilot.activities.developer import DeveloperActivities, DevelopPatchInput, _build_patch
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.plans import (
    DeveloperContext,
    DeveloperFileEdit,
    DeveloperPatchDesign,
    PatchProposal,
    PlannedFileChange,
    WorkItem,
)
from repopilot.domain.tasks import BudgetInput, TaskSpec
from repopilot.infrastructure.model.fake import FakeModelProvider
from repopilot.services.model_gateway import BudgetedModelGateway
from tests.fakes import MemoryArtifactStore, RecordingBudgetStore


def _archive(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def test_generated_patch_normalizes_crlf_checkout() -> None:
    patch = _build_patch(
        DeveloperPatchDesign(
            edits=(
                DeveloperFileEdit(
                    path="calc.py",
                    operation="modify",
                    content="def add(a, b):\n    return a + b\n",
                ),
            ),
            rationale="fix addition",
        ),
        {"calc.py": "def add(a, b):\r\n    return a - b\r\n"},
    )
    assert b"\r" not in patch
    assert b"-    return a - b\n" in patch


async def _task_ref(
    store: MemoryArtifactStore,
    metadata: ArtifactMetadata,
    tenant_id: UUID,
    run_id: UUID,
) -> ArtifactRef:
    task = TaskSpec(
        task_id=uuid4(),
        run_id=run_id,
        tenant_id=tenant_id,
        repository_url="https://github.com/owner/repo",
        base_revision="a" * 40,
        requirement="implement the requested change",
        acceptance_criteria=("behavior is updated",),
        acceptance_criteria_source="structured",
        policy_profile="default",
        budget=BudgetInput(
            max_cost_usd=Decimal("1"),
            max_wall_time_seconds=600,
            max_model_calls=10,
            max_sandbox_seconds=300,
        ),
    )
    return await store.put_bytes(
        ArtifactKind.TASK_SPEC,
        task.model_dump_json().encode(),
        metadata,
    )


async def test_developer_builds_scoped_patch_without_exposing_other_files() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id, item_id = uuid4(), uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        _archive(
            {
                "src/app.py": b"def value():\n    return 1\n",
                ".repopilot/sealed_tests/test_secret.py": b"SECRET_MARKER = True\n",
            }
        ),
        metadata,
    )
    context_ref = await store.put_bytes(
        ArtifactKind.DEVELOPER_CONTEXT,
        DeveloperContext(
            work_item=WorkItem(
                work_item_id=item_id,
                run_id=run_id,
                kind="code",
                dependencies=(),
                allowed_write_paths=("src/app.py",),
                read_paths=("src/app.py",),
                owner="developer-1",
                attempt=1,
            ),
            input_revision="a" * 40,
            source_archive_ref=source_ref,
            upstream_interface_refs=(),
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    task_ref = await _task_ref(store, metadata, tenant_id, run_id)
    planned = PlannedFileChange(
        work_item_id=item_id,
        path="src/app.py",
        operation="modify",
        owner="developer-1",
        responsibility="fix return value",
        required_interfaces=(),
    )
    provider = FakeModelProvider(
        [
            DeveloperPatchDesign(
                edits=(
                    DeveloperFileEdit(
                        path="src/app.py",
                        operation="modify",
                        content="def value():\n    return 2\n",
                    ),
                ),
                rationale="Implement the requested behavior.",
            )
        ],
        store,
    )
    developer = DeveloperActivities(
        artifact_store=store,
        gateway=BudgetedModelGateway(provider, RecordingBudgetStore()),
        model="fake-model",
        reservation_usd=Decimal("0.10"),
    )

    proposal_ref = await developer.develop_patch(
        DevelopPatchInput(
            developer_context_ref=context_ref,
            task_spec_ref=task_ref,
            planned_files=(planned,),
        )
    )
    caller = ArtifactCaller(
        tenant_id=tenant_id,
        run_id=run_id,
        role=None,
        service="test",
    )
    proposal = PatchProposal.model_validate_json(await store.get_bytes(proposal_ref, caller))
    patch = (await store.get_bytes(proposal.patch_ref, caller)).decode()
    trajectory = json.loads(await store.get_bytes(provider.requests[0].messages_ref, caller))

    assert proposal.work_item_id == item_id
    assert proposal.touched_paths == ("src/app.py",)
    assert "-    return 1" in patch
    assert "+    return 2" in patch
    assert "SECRET_MARKER" not in json.dumps(trajectory)


async def test_developer_rejects_unapproved_dependency_change_before_model_call() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id, item_id = uuid4(), uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    source_ref = await store.put_bytes(
        ArtifactKind.SOURCE_ARCHIVE,
        _archive({"pyproject.toml": b'[project]\nname = "demo"\n'}),
        metadata,
    )
    context_ref = await store.put_bytes(
        ArtifactKind.DEVELOPER_CONTEXT,
        DeveloperContext(
            work_item=WorkItem(
                work_item_id=item_id,
                run_id=run_id,
                kind="code",
                dependencies=(),
                allowed_write_paths=("pyproject.toml",),
                read_paths=("pyproject.toml",),
                owner="developer-1",
                attempt=1,
            ),
            input_revision="a" * 40,
            source_archive_ref=source_ref,
            upstream_interface_refs=(),
        )
        .model_dump_json()
        .encode(),
        metadata,
    )
    task_ref = await _task_ref(store, metadata, tenant_id, run_id)
    provider = FakeModelProvider([], store)

    with pytest.raises(ApplicationError):
        await DeveloperActivities(
            artifact_store=store,
            gateway=BudgetedModelGateway(provider, RecordingBudgetStore()),
            model="fake-model",
            reservation_usd=Decimal("0.10"),
        ).develop_patch(
            DevelopPatchInput(
                developer_context_ref=context_ref,
                task_spec_ref=task_ref,
                planned_files=(
                    PlannedFileChange(
                        work_item_id=item_id,
                        path="pyproject.toml",
                        operation="modify",
                        owner="developer-1",
                        responsibility="change package",
                        required_interfaces=(),
                    ),
                ),
            )
        )
    assert provider.requests == []

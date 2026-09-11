"""`tests/security` 专属 fixture：真实 Docker + 真实 MinIO/PostgreSQL。"""

from __future__ import annotations

import io
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest_asyncio

from repopilot.domain.artifacts import ArtifactMetadata
from repopilot.domain.enums import ArtifactKind
from repopilot.infrastructure.artifacts.minio import MinioArtifactStore
from repopilot.infrastructure.sandbox.docker import DockerSandboxService

SANDBOX_IMAGE = "python:3.12-slim"
TENANT_ID = uuid4()


def make_tar_gz(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest_asyncio.fixture
async def sandbox_service(
    tmp_path: Path, artifact_store: MinioArtifactStore
) -> AsyncIterator[DockerSandboxService]:
    service = DockerSandboxService(
        data_dir=tmp_path / "repopilot-data", artifact_store=artifact_store, tenant_id=TENANT_ID
    )
    yield service


@pytest_asyncio.fixture
async def source_archive_ref(artifact_store: MinioArtifactStore):
    run_id = uuid4()
    content = make_tar_gz({"main.py": "print('hello')\n"})
    metadata = ArtifactMetadata(
        tenant_id=TENANT_ID, run_id=run_id, base_revision="a" * 40, schema_version="1"
    )
    ref = await artifact_store.put_bytes(ArtifactKind.SOURCE_ARCHIVE, content, metadata)
    return run_id, ref

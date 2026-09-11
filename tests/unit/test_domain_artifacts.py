"""ArtifactRef 的校验规则（实施设计 7.2 节）。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from repopilot.domain.artifacts import ArtifactRef, build_object_key
from repopilot.domain.enums import ArtifactKind

VALID_SHA256 = "a" * 64
VALID_COMMIT_SHA = "b" * 40


def _make_ref(**overrides: object) -> ArtifactRef:
    tenant_id = overrides.pop("tenant_id", uuid4())
    run_id = overrides.pop("run_id", uuid4())
    kind = overrides.pop("kind", ArtifactKind.PATCH)
    sha256 = overrides.pop("sha256", VALID_SHA256)
    base_revision = overrides.pop("base_revision", VALID_COMMIT_SHA)
    fields = {
        "artifact_id": uuid4(),
        "run_id": run_id,
        "tenant_id": tenant_id,
        "kind": kind,
        "schema_version": "1",
        "object_key": build_object_key(tenant_id, run_id, kind, sha256),
        "sha256": sha256,
        "size_bytes": 10,
        "base_revision": base_revision,
        "input_artifact_ids": (),
        "created_at": datetime.now(UTC),
        **overrides,
    }
    return ArtifactRef(**fields)  # type: ignore[arg-type]


def test_valid_patch_ref_round_trips() -> None:
    ref = _make_ref()
    assert ref.base_revision == VALID_COMMIT_SHA


def test_create_run_request_may_omit_base_revision() -> None:
    ref = _make_ref(kind=ArtifactKind.CREATE_RUN_REQUEST, base_revision=None)
    assert ref.base_revision is None


def test_non_create_run_request_requires_full_sha() -> None:
    with pytest.raises(ValidationError):
        _make_ref(base_revision=None)
    with pytest.raises(ValidationError):
        _make_ref(base_revision="not-a-sha")


def test_sha256_must_be_hex64() -> None:
    with pytest.raises(ValidationError):
        _make_ref(sha256="too-short")


def test_size_bytes_cannot_be_negative() -> None:
    with pytest.raises(ValidationError):
        _make_ref(size_bytes=-1)


def test_build_object_key_format() -> None:
    tenant_id, run_id = uuid4(), uuid4()
    key = build_object_key(tenant_id, run_id, ArtifactKind.PATCH, VALID_SHA256)
    assert key == f"{tenant_id}/{run_id}/patch/{VALID_SHA256}"

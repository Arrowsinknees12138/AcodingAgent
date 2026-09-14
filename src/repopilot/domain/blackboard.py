"""Immutable, source-linked information shared across workflow agents."""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from repopilot.domain import StrictModel
from repopilot.domain.enums import ArtifactKind


class BlackboardEntry(StrictModel):
    author: str
    source_kind: ArtifactKind
    source_artifact_id: UUID
    summary: str = Field(min_length=1, max_length=8_000)


class BlackboardState(StrictModel):
    entries: tuple[BlackboardEntry, ...] = Field(max_length=20)

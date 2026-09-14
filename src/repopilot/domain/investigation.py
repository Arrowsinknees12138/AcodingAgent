"""Evidence-backed root cause analysis shared with repair agents."""

from __future__ import annotations

from pydantic import Field

from repopilot.domain import StrictModel


class InvestigationEvidence(StrictModel):
    path: str
    line: int = Field(ge=1)
    observation: str = Field(min_length=1, max_length=1_000)


class InvestigationReport(StrictModel):
    root_cause: str = Field(min_length=1, max_length=4_000)
    evidence: tuple[InvestigationEvidence, ...] = Field(min_length=1, max_length=12)
    suggested_paths: tuple[str, ...] = Field(max_length=5)
    recommendation: str = Field(min_length=1, max_length=2_000)
    confidence: float = Field(ge=0, le=1)

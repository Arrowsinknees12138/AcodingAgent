"""验证 StrictModel 的 strict/extra/frozen 行为（实施设计 23.1 节）。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from repopilot.domain import StrictModel


class _Example(StrictModel):
    count: int
    name: str


def test_strict_rejects_implicit_type_coercion() -> None:
    with pytest.raises(ValidationError):
        _Example(count="1", name="a")  # type: ignore[arg-type]


def test_extra_forbid_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _Example(count=1, name="a", unexpected=True)  # type: ignore[call-arg]


def test_frozen_rejects_mutation_after_creation() -> None:
    example = _Example(count=1, name="a")
    with pytest.raises(ValidationError):
        example.count = 2  # type: ignore[misc]

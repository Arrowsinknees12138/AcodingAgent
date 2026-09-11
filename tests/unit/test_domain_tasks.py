"""CreateRunRequest / TaskSpec 校验规则（实施设计 7.1 节）。"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from repopilot.domain.tasks import (
    MAX_COST_USD,
    BudgetInput,
    CreateRunRequest,
    RepositoryInput,
    TaskSpec,
)

VALID_BUDGET = BudgetInput(
    max_cost_usd=Decimal("5"),
    max_wall_time_seconds=600,
    max_model_calls=10,
    max_sandbox_seconds=300,
)


def test_repository_input_rejects_non_github_url() -> None:
    with pytest.raises(ValidationError):
        RepositoryInput(url="https://gitlab.com/owner/repo")


def test_repository_input_accepts_public_github_url() -> None:
    repo = RepositoryInput(url="https://github.com/owner/repo")
    assert repo.revision is None


def test_budget_must_be_positive_and_within_server_cap() -> None:
    with pytest.raises(ValidationError):
        BudgetInput(
            max_cost_usd=Decimal("0"),
            max_wall_time_seconds=600,
            max_model_calls=10,
            max_sandbox_seconds=300,
        )
    with pytest.raises(ValidationError):
        BudgetInput(
            max_cost_usd=MAX_COST_USD + 1,
            max_wall_time_seconds=600,
            max_model_calls=10,
            max_sandbox_seconds=300,
        )


def test_create_run_request_allows_missing_acceptance_criteria() -> None:
    request = CreateRunRequest(
        repository=RepositoryInput(url="https://github.com/owner/repo"),
        requirement="fix the bug",
        acceptance_criteria=None,
        budget=VALID_BUDGET,
    )
    assert request.acceptance_criteria is None


def test_create_run_request_rejects_too_many_acceptance_criteria() -> None:
    with pytest.raises(ValidationError):
        CreateRunRequest(
            repository=RepositoryInput(url="https://github.com/owner/repo"),
            requirement="fix the bug",
            acceptance_criteria=tuple(f"criterion {i}" for i in range(101)),
            budget=VALID_BUDGET,
        )


def test_task_spec_requires_full_commit_sha() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id=uuid4(),
            run_id=uuid4(),
            tenant_id=uuid4(),
            repository_url="https://github.com/owner/repo",
            base_revision="main",
            requirement="fix",
            acceptance_criteria=("must pass",),
            acceptance_criteria_source="structured",
            policy_profile="default",
            budget=VALID_BUDGET,
        )


def test_task_spec_requires_non_empty_acceptance_criteria() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(
            task_id=uuid4(),
            run_id=uuid4(),
            tenant_id=uuid4(),
            repository_url="https://github.com/owner/repo",
            base_revision="a" * 40,
            requirement="fix",
            acceptance_criteria=(),
            acceptance_criteria_source="structured",
            policy_profile="default",
            budget=VALID_BUDGET,
        )

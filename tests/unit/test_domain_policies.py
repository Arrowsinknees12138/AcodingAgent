"""风险审批、依赖与 QA 策略的纯领域测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from repopilot.domain.enums import (
    ApprovalMode,
    ApprovalPolicyMode,
    RiskFlag,
    RiskLevel,
)
from repopilot.domain.policies import (
    ApprovalPolicy,
    ApprovalStagePolicy,
    DependencyPolicy,
    QaPolicy,
    classify_risk,
    dependency_manifest_paths,
    resolve_approval_policy,
)


@pytest.mark.parametrize(
    ("flags", "expected"),
    [
        ((), RiskLevel.LOW),
        ((RiskFlag.DEPENDENCY_CHANGE,), RiskLevel.MEDIUM),
        ((RiskFlag.CI_CONFIG,), RiskLevel.MEDIUM),
        ((RiskFlag.AUTH_CHANGE,), RiskLevel.HIGH),
        (
            (RiskFlag.LARGE_SCOPE, RiskFlag.PROMPT_INJECTION_SUSPECTED),
            RiskLevel.HIGH,
        ),
    ],
)
def test_classify_risk_uses_highest_matching_level(
    flags: tuple[RiskFlag, ...], expected: RiskLevel
) -> None:
    assert classify_risk(flags) is expected


def test_risk_based_approval_matrix() -> None:
    low = resolve_approval_policy(RiskLevel.LOW, ApprovalPolicy())
    medium = resolve_approval_policy(RiskLevel.MEDIUM, ApprovalPolicy())
    high = resolve_approval_policy(RiskLevel.HIGH, ApprovalPolicy())

    assert low.stages == ApprovalStagePolicy()
    assert medium.stages.plan is ApprovalMode.MANUAL
    assert medium.stages.execution is ApprovalMode.AUTOMATIC
    assert high.stages.plan is ApprovalMode.MANUAL
    assert high.stages.execution is ApprovalMode.MANUAL
    assert high.stages.delivery is ApprovalMode.AUTOMATIC


def test_custom_approval_policy_is_used_verbatim() -> None:
    custom = ApprovalStagePolicy(
        plan=ApprovalMode.AUTOMATIC,
        execution=ApprovalMode.MANUAL,
        delivery=ApprovalMode.MANUAL,
    )
    resolved = resolve_approval_policy(
        RiskLevel.LOW,
        ApprovalPolicy(mode=ApprovalPolicyMode.CUSTOM, custom=custom),
    )
    assert resolved.source == "custom"
    assert resolved.stages == custom


def test_custom_mode_requires_custom_stage_settings() -> None:
    with pytest.raises(ValidationError):
        ApprovalPolicy(mode=ApprovalPolicyMode.CUSTOM)


def test_dependency_policy_is_official_pypi_lockfile_and_cache_by_default() -> None:
    policy = DependencyPolicy()
    assert policy.index_url == "https://pypi.org/simple"
    assert policy.allow_lockfile_read
    assert policy.allow_cache
    assert not policy.allow_new_dependencies


def test_standard_qa_cannot_omit_required_check() -> None:
    with pytest.raises(ValidationError):
        QaPolicy(required=("acceptance_tests", "targeted_tests"))


def test_dependency_manifests_are_detected_across_supported_layouts() -> None:
    assert dependency_manifest_paths(
        (
            "src/app.py",
            "pyproject.toml",
            "requirements-dev.txt",
            "config/requirements.in",
            ".\\uv.lock",
        )
    ) == (
        "config/requirements.in",
        "pyproject.toml",
        "requirements-dev.txt",
        "uv.lock",
    )

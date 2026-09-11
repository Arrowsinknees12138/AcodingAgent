"""`check_read_access` 的纯函数单元测试（实施设计 18.1 节矩阵）。"""

from __future__ import annotations

from uuid import uuid4

from repopilot.domain.artifacts import ArtifactCaller
from repopilot.domain.enums import AgentRole, ArtifactKind
from repopilot.services.artifact_store import check_read_access


def _caller(role: AgentRole | None, service: str = "agent-loop") -> ArtifactCaller:
    return ArtifactCaller(tenant_id=uuid4(), run_id=uuid4(), role=role, service=service)


def test_trusted_service_always_allowed() -> None:
    caller = _caller(role=None, service="repository")
    assert check_read_access(ArtifactKind.TEST_BUNDLE, caller) is True
    assert check_read_access(ArtifactKind.SOURCE_ARCHIVE, caller) is True


def test_developer_can_read_source_archive_but_not_test_bundle() -> None:
    caller = _caller(role=AgentRole.DEVELOPER)
    assert check_read_access(ArtifactKind.SOURCE_ARCHIVE, caller) is True
    assert check_read_access(ArtifactKind.TEST_BUNDLE, caller) is False


def test_reviewer_cannot_read_repository_snapshot() -> None:
    caller = _caller(role=AgentRole.REVIEWER)
    assert check_read_access(ArtifactKind.REPOSITORY_SNAPSHOT, caller) is False
    assert check_read_access(ArtifactKind.CHANGE_PLAN, caller) is True


def test_unknown_kind_denied_for_agent_roles() -> None:
    caller = _caller(role=AgentRole.QA)
    assert check_read_access(ArtifactKind.TRAJECTORY, caller) is False

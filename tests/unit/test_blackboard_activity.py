from __future__ import annotations

from uuid import uuid4

import pytest
from temporalio.exceptions import ApplicationError

from repopilot.activities.blackboard import BlackboardActivities, PublishBlackboardInput
from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata
from repopilot.domain.blackboard import BlackboardState
from repopilot.domain.enums import ArtifactKind
from repopilot.domain.investigation import InvestigationEvidence, InvestigationReport
from repopilot.domain.plans import ChangePlan, PlannedFileChange
from tests.fakes import MemoryArtifactStore


async def test_blackboard_shares_safe_source_linked_summaries() -> None:
    store = MemoryArtifactStore()
    tenant_id, run_id = uuid4(), uuid4()
    metadata = ArtifactMetadata(
        tenant_id=tenant_id,
        run_id=run_id,
        base_revision="a" * 40,
        schema_version="1",
    )
    plan = ChangePlan(
        plan_id=uuid4(),
        version=1,
        supersedes_plan_id=None,
        files=(
            PlannedFileChange(
                work_item_id=uuid4(),
                path="calc.py",
                operation="modify",
                owner="developer",
                responsibility="fix addition",
                required_interfaces=(),
            ),
        ),
        dependency_edges=(),
        risk_flags=(),
    )
    plan_ref = await store.put_bytes(
        ArtifactKind.CHANGE_PLAN, plan.model_dump_json().encode(), metadata
    )
    investigation = InvestigationReport(
        root_cause="The add function subtracts",
        evidence=(InvestigationEvidence(path="calc.py", line=2, observation="minus operator"),),
        suggested_paths=("calc.py",),
        recommendation="Use plus",
        confidence=0.9,
    )
    investigation_ref = await store.put_bytes(
        ArtifactKind.INVESTIGATION, investigation.model_dump_json().encode(), metadata
    )
    publisher = BlackboardActivities(artifact_store=store)
    first_ref = await publisher.publish_blackboard(PublishBlackboardInput(source_ref=plan_ref))
    second_ref = await publisher.publish_blackboard(
        PublishBlackboardInput(source_ref=investigation_ref, previous_ref=first_ref)
    )
    caller = ArtifactCaller(tenant_id=tenant_id, run_id=run_id, role=None, service="test")
    first = BlackboardState.model_validate_json(await store.get_bytes(first_ref, caller))
    second = BlackboardState.model_validate_json(await store.get_bytes(second_ref, caller))
    assert len(first.entries) == 1
    assert [entry.author for entry in second.entries] == ["planner", "investigator"]
    assert second.entries[1].source_artifact_id == investigation_ref.artifact_id
    assert "The add function subtracts" in second.entries[1].summary
    with pytest.raises(ApplicationError, match="already published"):
        await publisher.publish_blackboard(
            PublishBlackboardInput(source_ref=investigation_ref, previous_ref=second_ref)
        )

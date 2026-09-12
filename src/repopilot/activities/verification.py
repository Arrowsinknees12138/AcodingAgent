"""Verification Service 的 Temporal Activity 适配层。"""

from __future__ import annotations

from temporalio import activity

from repopilot.domain.artifacts import ArtifactRef
from repopilot.services.verification_service import BaselineVerificationService


class VerificationActivities:
    def __init__(self, baseline: BaselineVerificationService) -> None:
        self._baseline = baseline

    @activity.defn(name="verify_baseline")
    async def verify_baseline(self, snapshot_ref: ArtifactRef) -> ArtifactRef:
        return await self._baseline.verify(snapshot_ref)

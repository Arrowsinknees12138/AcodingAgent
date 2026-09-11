"""ChangePlan / WorkItem / Developer 产物契约（实施设计第 7.3、7.4 节）。

DAG 校验规则（唯一 ID、path 唯一 Owner、无环、写路径来自 ChangePlan 等，
第 7.3 节末尾列出的规则）不是"字段级"校验，而是"跨 WorkItem 集合"的
校验，属于 Dependency Scheduler（Milestone 7）的职责，不适合放进单个
Pydantic Model 里——这里只固定数据结构本身。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from repopilot.domain import StrictModel
from repopilot.domain.artifacts import ArtifactRef
from repopilot.domain.enums import RiskFlag


class PlannedFileChange(StrictModel):
    work_item_id: UUID
    path: str
    operation: Literal["create", "modify", "delete"]
    owner: str
    responsibility: str
    required_interfaces: tuple[str, ...]


class ChangePlan(StrictModel):
    plan_id: UUID
    version: int
    supersedes_plan_id: UUID | None
    files: tuple[PlannedFileChange, ...]
    dependency_edges: tuple[tuple[UUID, UUID], ...]
    risk_flags: tuple[RiskFlag, ...]


class WorkItem(StrictModel):
    work_item_id: UUID
    run_id: UUID
    kind: Literal["code", "repair"]
    dependencies: tuple[UUID, ...]
    allowed_write_paths: tuple[str, ...]
    read_paths: tuple[str, ...]
    owner: str
    attempt: int


class DeveloperContext(StrictModel):
    work_item: WorkItem
    input_revision: str
    source_archive_ref: ArtifactRef
    upstream_interface_refs: tuple[ArtifactRef, ...]


class PatchProposal(StrictModel):
    work_item_id: UUID
    attempt: int
    input_revision: str
    patch_ref: ArtifactRef
    touched_paths: tuple[str, ...]


class IntegratedPatch(StrictModel):
    work_item_id: UUID
    proposal_ref: ArtifactRef
    input_revision: str
    commit_sha: str
    exported_interface_ref: ArtifactRef

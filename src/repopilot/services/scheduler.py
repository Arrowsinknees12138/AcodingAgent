"""ChangePlan / WorkItem DAG 校验与确定性分批调度。"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from uuid import UUID

from pydantic import Field

from repopilot.domain import StrictModel
from repopilot.domain.plans import ChangePlan, WorkItem


class InvalidPlanError(ValueError):
    pass


class SchedulePlan(StrictModel):
    waves: tuple[tuple[UUID, ...], ...]
    max_concurrency: int = Field(gt=0, le=4)


def normalize_plan_path(path: str) -> str:
    normalized = posixpath.normpath(path.replace("\\", "/")).removeprefix("./")
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        raise InvalidPlanError(f"非法计划路径: {path!r}")
    if normalized.startswith("/"):
        raise InvalidPlanError(f"计划路径必须是仓库相对路径: {path!r}")
    return normalized


def build_schedule(
    plan: ChangePlan,
    work_items: tuple[WorkItem, ...],
    *,
    max_concurrency: int = 4,
) -> SchedulePlan:
    if not 1 <= max_concurrency <= 4:
        raise InvalidPlanError("Phase 0 Developer 并发数必须在 1..4")
    if len(plan.files) > 20:
        raise InvalidPlanError("Phase 0 单个计划最多修改 20 个文件")

    item_ids = tuple(item.work_item_id for item in work_items)
    if len(set(item_ids)) != len(item_ids):
        raise InvalidPlanError("WorkItem ID 必须唯一")
    item_by_id = {item.work_item_id: item for item in work_items}

    paths: dict[str, UUID] = {}
    planned_paths_by_item: dict[UUID, set[str]] = defaultdict(set)
    owner_by_item: dict[UUID, str] = {}
    deleted_paths: set[str] = set()
    required_interfaces: set[str] = set()
    for file in plan.files:
        if file.work_item_id not in item_by_id:
            raise InvalidPlanError(f"计划文件引用了不存在的 WorkItem: {file.work_item_id}")
        path = normalize_plan_path(file.path)
        if path in paths:
            raise InvalidPlanError(f"规范化后路径重复或有多个 Owner: {path}")
        paths[path] = file.work_item_id
        planned_paths_by_item[file.work_item_id].add(path)
        previous_owner = owner_by_item.setdefault(file.work_item_id, file.owner)
        if previous_owner != file.owner or item_by_id[file.work_item_id].owner != file.owner:
            raise InvalidPlanError(f"WorkItem {file.work_item_id} 的 Owner 不一致")
        if file.operation == "delete":
            deleted_paths.add(path)
        required_interfaces.update(normalize_plan_path(value) for value in file.required_interfaces)
    invalid_deleted_interfaces = deleted_paths.intersection(required_interfaces)
    if invalid_deleted_interfaces:
        raise InvalidPlanError(
            "删除文件仍被下游接口引用: " + ", ".join(sorted(invalid_deleted_interfaces))
        )

    for item in work_items:
        actual = {normalize_plan_path(path) for path in item.allowed_write_paths}
        planned = planned_paths_by_item.get(item.work_item_id, set())
        if actual != planned:
            raise InvalidPlanError(
                f"WorkItem {item.work_item_id} 的 allowed_write_paths 与 ChangePlan 不一致"
            )

    adjacency: dict[UUID, set[UUID]] = {item_id: set() for item_id in item_ids}
    indegree: dict[UUID, int] = {item_id: 0 for item_id in item_ids}
    edge_dependencies: dict[UUID, set[UUID]] = defaultdict(set)
    for upstream, downstream in plan.dependency_edges:
        if upstream not in item_by_id or downstream not in item_by_id:
            raise InvalidPlanError("dependency edge 两端必须引用现有 WorkItem")
        if downstream not in adjacency[upstream]:
            adjacency[upstream].add(downstream)
            indegree[downstream] += 1
            edge_dependencies[downstream].add(upstream)

    for item in work_items:
        if set(item.dependencies) != edge_dependencies.get(item.work_item_id, set()):
            raise InvalidPlanError(f"WorkItem {item.work_item_id} 的 dependencies 与 DAG 边不一致")

    ready = sorted((item_id for item_id, degree in indegree.items() if degree == 0), key=str)
    waves: list[tuple[UUID, ...]] = []
    processed = 0
    while ready:
        wave = tuple(ready[:max_concurrency])
        ready = ready[max_concurrency:]
        waves.append(wave)
        processed += len(wave)
        for upstream in wave:
            for downstream in sorted(adjacency[upstream], key=str):
                indegree[downstream] -= 1
                if indegree[downstream] == 0:
                    ready.append(downstream)
        ready.sort(key=str)

    if processed != len(work_items):
        raise InvalidPlanError("ChangePlan dependency graph 存在环")
    return SchedulePlan(waves=tuple(waves), max_concurrency=max_concurrency)

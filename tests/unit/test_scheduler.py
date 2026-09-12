"""DAG 跨对象约束与最多四路的确定性调度。"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from repopilot.domain.enums import RiskFlag
from repopilot.domain.plans import ChangePlan, PlannedFileChange, WorkItem
from repopilot.services.scheduler import InvalidPlanError, build_schedule

RUN_ID = uuid4()


def _file(item_id: UUID, path: str, *, owner: str, operation: str = "modify") -> PlannedFileChange:
    return PlannedFileChange.model_validate(
        {
            "work_item_id": item_id,
            "path": path,
            "operation": operation,
            "owner": owner,
            "responsibility": "implement",
            "required_interfaces": (),
        }
    )


def _item(item_id: UUID, path: str, *, owner: str, dependencies: tuple[UUID, ...] = ()) -> WorkItem:
    return WorkItem(
        work_item_id=item_id,
        run_id=RUN_ID,
        kind="code",
        dependencies=dependencies,
        allowed_write_paths=(path,),
        read_paths=(),
        owner=owner,
        attempt=1,
    )


def _plan(
    files: tuple[PlannedFileChange, ...], edges: tuple[tuple[UUID, UUID], ...] = ()
) -> ChangePlan:
    return ChangePlan(
        plan_id=uuid4(),
        version=1,
        supersedes_plan_id=None,
        files=files,
        dependency_edges=edges,
        risk_flags=(RiskFlag.LARGE_SCOPE,),
    )


def test_schedule_releases_downstream_only_after_upstreams() -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    items = (
        _item(first, "src/a.py", owner="dev-a"),
        _item(second, "src/b.py", owner="dev-b"),
        _item(third, "src/c.py", owner="dev-c", dependencies=(first, second)),
    )
    schedule = build_schedule(
        _plan(
            (
                _file(first, "src/a.py", owner="dev-a"),
                _file(second, "src/b.py", owner="dev-b"),
                _file(third, "src/c.py", owner="dev-c"),
            ),
            ((first, third), (second, third)),
        ),
        items,
    )
    assert set(schedule.waves[0]) == {first, second}
    assert schedule.waves[1] == (third,)


def test_schedule_chunks_independent_work_at_four() -> None:
    ids = tuple(uuid4() for _ in range(6))
    files = tuple(
        _file(item_id, f"src/{index}.py", owner=f"dev-{index}") for index, item_id in enumerate(ids)
    )
    items = tuple(
        _item(item_id, f"src/{index}.py", owner=f"dev-{index}") for index, item_id in enumerate(ids)
    )
    schedule = build_schedule(_plan(files), items)
    assert tuple(len(wave) for wave in schedule.waves) == (4, 2)


def test_cycle_is_rejected() -> None:
    first, second = uuid4(), uuid4()
    with pytest.raises(InvalidPlanError, match="存在环"):
        build_schedule(
            _plan(
                (
                    _file(first, "src/a.py", owner="dev-a"),
                    _file(second, "src/b.py", owner="dev-b"),
                ),
                ((first, second), (second, first)),
            ),
            (
                _item(first, "src/a.py", owner="dev-a", dependencies=(second,)),
                _item(second, "src/b.py", owner="dev-b", dependencies=(first,)),
            ),
        )


def test_normalized_duplicate_path_is_rejected() -> None:
    first, second = uuid4(), uuid4()
    with pytest.raises(InvalidPlanError, match="路径重复"):
        build_schedule(
            _plan(
                (
                    _file(first, "src/./app.py", owner="dev-a"),
                    _file(second, "src/app.py", owner="dev-b"),
                )
            ),
            (
                _item(first, "src/app.py", owner="dev-a"),
                _item(second, "src/app.py", owner="dev-b"),
            ),
        )


def test_allowed_write_paths_must_match_planned_ownership() -> None:
    item_id = uuid4()
    with pytest.raises(InvalidPlanError, match="allowed_write_paths"):
        build_schedule(
            _plan((_file(item_id, "src/app.py", owner="dev-a"),)),
            (_item(item_id, "src/other.py", owner="dev-a"),),
        )

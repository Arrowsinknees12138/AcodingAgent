"""`update_projection` Temporal Activity（实施设计第 8.4 节）。

Activity 本身只做一件事：把已经校验好的 `ProjectionEvent` 转交给注入的
`RunProjectionStore`。依赖方向上，activities 调用 services 提供的接口，
不直接依赖某个具体的基础设施实现——具体用 PostgreSQL 还是别的存储，由
Worker 启动时注入决定（见 infrastructure/temporal/workers.py）。
"""

from __future__ import annotations

from temporalio import activity

from repopilot.services.run_projection import ProjectionEvent, RunProjectionStore


class ProjectionActivities:
    def __init__(self, store: RunProjectionStore) -> None:
        self._store = store

    @activity.defn(name="update_projection")
    async def update_projection(self, event: ProjectionEvent) -> None:
        await self._store.apply(event)

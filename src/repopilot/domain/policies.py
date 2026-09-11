"""Policy Engine 的输入/输出契约（实施设计第 16 节）。

Policy Engine 本身必须是纯函数（输入 -> 输出，不做 I/O）；具体的
`authorize()` 实现和 YAML 规则加载放在 `services/policy_engine.py`，
这里只固定它的输入输出数据结构，保持 domain 层不依赖任何框架。
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from repopilot.domain import StrictModel


class PolicyContext(StrictModel):
    tenant_id: UUID
    run_id: UUID
    work_item_id: UUID | None
    policy_version: str
    read_paths: tuple[str, ...]
    write_paths: tuple[str, ...]


class PolicyAction(StrictModel):
    kind: Literal["read", "write", "execute", "artifact_read"]
    resource: str
    arguments: tuple[str, ...] = ()


class PolicyDecision(StrictModel):
    allowed: bool
    reason_code: str
    normalized_resource: str

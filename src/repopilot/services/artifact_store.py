"""Artifact Store 的服务协议与读取 ACL（实施设计第 18 节）。

`ArtifactStore` 是一个 Protocol：具体实现（MinIO + PostgreSQL 元数据）
放在 `infrastructure/artifacts/minio.py`，这里只定义接口和纯函数式的
访问控制规则，保持 `services` 层不直接依赖 MinIO/SQLAlchemy。

ACL 的粒度说明：`check_read_access()` 只做"这个角色/服务能不能读这一
*类* Artifact"的粗粒度判断，对应设计文档 18.1 节矩阵里的"是/否"。矩阵中
"当前 WorkItem"“经审批只读”“post-patch 阶段读”这类需要具体实例上下文
（当前处理的是哪个 WorkItem、是否已批准豁免）的细粒度限制，由发起读取
的调用方（context_builder、tool registry）在决定"要不要请求这个 ArtifactRef"
时自行限定，不在这里重复实现——ArtifactStore 层面拿不到那些上下文，假装
在这里做细粒度校验只会给人"已经安全"的错觉。
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from repopilot.domain.artifacts import ArtifactCaller, ArtifactMetadata, ArtifactRef, CleanupResult
from repopilot.domain.enums import AgentRole, ArtifactKind

# 允许"直接以 Agent 身份"读取的 Artifact 种类。不在这个表里的 kind，
# 任何 Agent 角色一律拒绝（fail closed）；可信内部服务（caller.role is None）
# 不受此表限制，因为它们是产生/搬运 Artifact 内容的一方。
_AGENT_READABLE_KINDS: dict[ArtifactKind, frozenset[AgentRole]] = {
    ArtifactKind.REPOSITORY_SNAPSHOT: frozenset(
        {AgentRole.PLANNER, AgentRole.QA, AgentRole.DEVELOPER, AgentRole.INVESTIGATOR}
    ),
    ArtifactKind.SOURCE_ARCHIVE: frozenset({AgentRole.DEVELOPER}),
    ArtifactKind.CHANGE_PLAN: frozenset(
        {AgentRole.PLANNER, AgentRole.QA, AgentRole.DEVELOPER, AgentRole.REVIEWER}
    ),
    ArtifactKind.TEST_BUNDLE: frozenset({AgentRole.QA, AgentRole.REVIEWER}),
    ArtifactKind.PATCH: frozenset({AgentRole.QA, AgentRole.DEVELOPER, AgentRole.REVIEWER}),
    ArtifactKind.VERIFICATION_REPORT: frozenset(
        {AgentRole.QA, AgentRole.DEVELOPER, AgentRole.REVIEWER}
    ),
    ArtifactKind.INVESTIGATION: frozenset(
        {AgentRole.PLANNER, AgentRole.DEVELOPER, AgentRole.REVIEWER}
    ),
    ArtifactKind.BLACKBOARD: frozenset(
        {
            AgentRole.PLANNER,
            AgentRole.INVESTIGATOR,
            AgentRole.DEVELOPER,
            AgentRole.REVIEWER,
        }
    ),
}


def check_read_access(kind: ArtifactKind, caller: ArtifactCaller) -> bool:
    """粗粒度读取授权：见模块 docstring。"""
    if caller.role is None:
        return True
    allowed_roles = _AGENT_READABLE_KINDS.get(kind, frozenset())
    return caller.role in allowed_roles


class ArtifactStore(Protocol):
    async def put_bytes(
        self,
        kind: ArtifactKind,
        content: bytes,
        metadata: ArtifactMetadata,
    ) -> ArtifactRef: ...

    async def get_bytes(
        self,
        ref: ArtifactRef,
        caller: ArtifactCaller,
    ) -> bytes: ...

    async def delete_run(self, tenant_id: UUID, run_id: UUID) -> CleanupResult: ...

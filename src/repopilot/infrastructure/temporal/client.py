"""Temporal Client：连接 Server + 启动 CodeRepairWorkflow。

Workflow 标识规则（实施设计 8.1 节）：
- Workflow Type：`CodeRepairWorkflow`。
- Workflow ID：`repopilot/{tenant_id}/{run_id}`。
- 重复启动策略：Reject Duplicate——同一个 run_id 只允许存在一个 Workflow
  执行；重复调用 `start_code_repair_workflow` 会被 Temporal Server 拒绝，
  调用方（API 层）据此判断"这是同一个 Idempotency-Key 对应的已有 Run"。
"""

from __future__ import annotations

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy

from repopilot.config import Settings
from repopilot.infrastructure.temporal.converter import data_converter
from repopilot.services.task_queues import ORCHESTRATION_TASK_QUEUE
from repopilot.workflows.code_repair import (
    CodeRepairWorkflow,
    CodeRepairWorkflowInput,
    CodeRepairWorkflowOutput,
)


def workflow_id_for(tenant_id: object, run_id: object) -> str:
    return f"repopilot/{tenant_id}/{run_id}"


async def connect(settings: Settings) -> Client:
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        data_converter=data_converter,
    )


async def start_code_repair_workflow(
    client: Client, workflow_input: CodeRepairWorkflowInput
) -> WorkflowHandle[CodeRepairWorkflow, CodeRepairWorkflowOutput]:
    return await client.start_workflow(
        CodeRepairWorkflow.run,
        workflow_input,
        id=workflow_id_for(workflow_input.tenant_id, workflow_input.run_id),
        task_queue=ORCHESTRATION_TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
    )

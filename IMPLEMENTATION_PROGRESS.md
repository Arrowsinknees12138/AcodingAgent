# Agent 升级实施记录

按任务记录完成状态、验证与提交；未标为完成的项目不得当作已上线能力。

| 优先级 | 任务 | 状态 | 验证/备注 |
| --- | --- | --- | --- |
| P0 | Developer 接入真正 AgentLoop | 已完成 | 新 API Run 使用多轮工具调用；旧 Run 仍走旧 Activity；单元 147/147、工作流集成 19/19（不含单独的重启用例）通过 |
| P0 | Developer 全仓 search/read、受限 write | 已完成 | 对仓库快照全量搜索/读取；写入和删除仅限批准路径；测试命令在断网沙箱执行。Planner 漏报的目标文件仍须走 P1 Scope Expansion 才能修改 |
| P0 | 本地 E2E benchmark + metrics | 已实现，待环境验证 | 六个固定本地用例（含 AgentLoop）、JUnit → JSON 指标与基线对比；统计单元测试通过。当前 Docker named pipe 不存在，真实 E2E 尚未跑通 |
| P1 | Planner Agent 化 | 已实现，待环境验证 | 新 Run 走最多 8 轮的只读全仓 search/read → submit_plan → finish；服务端仍校验 DAG、路径与风险；旧 Run 保持原单次调用。单元测试通过，Docker E2E 待验证 |
| P1 | Scope Expansion 协议 | 已实现，待环境验证 | 新 Run 的 repair 可提出最多 3 个新路径，须说明理由；自动提升至至少 Medium 并重走计划审批；旧 Run 保持原 allowlist。单元测试通过，Docker E2E 待验证 |
| P1 | Investigator Agent | 已实现，待环境验证 | repair 前只读全仓 search/read，提交含根因、文件行证据、建议路径的独立调查产物；Planner/Developer 共享其结论，密封测试原始日志不暴露。单元测试通过，Docker E2E 待验证 |
| P1 | Shared Blackboard | 待完成 | |
| P2 | Reviewer Agent 化 | 待完成 | |

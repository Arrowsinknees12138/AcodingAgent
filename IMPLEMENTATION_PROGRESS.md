# Agent 升级实施记录

按任务记录完成状态、验证与提交；未标为完成的项目不得当作已上线能力。

| 优先级 | 任务 | 状态 | 验证/备注 |
| --- | --- | --- | --- |
| P0 | Developer 接入真正 AgentLoop | 已完成 | 新 API Run 使用多轮工具调用，旧 Run 保持旧 Activity；真实 E2E 多 Agent 场景通过 |
| P0 | Developer 全仓 search/read、受限 write | 已完成 | 全仓快照可搜索/读取；写入/删除仅限批准路径，命令在断网沙箱；真实 E2E 覆盖搜索、读取、写入、运行测试 |
| P0 | 本地 E2E benchmark + metrics | 已完成 | 七个固定本地场景 7/7 通过，JUnit → JSON、基线对比、模型调用/费用/沙箱执行耗时；基线见 `benchmarks/baselines/local-2026-09-14.json` |
| P1 | Planner Agent 化 | 已完成 | 新 Run 走只读全仓 search/read → submit_plan → finish；服务端仍校验 DAG、路径与风险；真实 E2E 通过 |
| P1 | Scope Expansion 协议 | 已完成 | repair 可提出最多 3 个新路径，须说明理由；风险至少提升为 Medium 并重走计划审批；旧 Run 保持原 allowlist。单元与真实 Temporal 集成用例覆盖新增路径、人工审批和执行 |
| P1 | Investigator Agent | 已完成 | repair 前只读调查、提交根因/文件行证据；结论进入 Planner/Developer；真实 E2E 的失败→调查→修复场景通过 |
| P1 | Shared Blackboard | 已完成 | 不可变来源摘要汇总计划/失败反馈/调查结论，供多个 Agent 共享；真实 E2E 通过；原始密封测试和日志不共享 |
| P2 | Reviewer Agent 化 | 已完成 | 新 Run 的 Reviewer 只读搜索/读取候选仓库并 submit_review；严格 BLOCK/COMMENT 契约；真实 E2E 通过 |

验证（2026-09-14）：单元 152/152、契约 13/13、集成 21/21、安全 8/8、真实 E2E benchmark 7/7，合计 201 个测试分别通过。上述 E2E 使用确定性假模型，不代表线上模型质量。生产 FinalReport 的 `sandbox_seconds` 仍未计量；benchmark 由测试侧累计 Docker 命令耗时。

# 第一阶段验证记录

日期：2026-10-03，Windows / Python 3.12，分支 `codex/ci-repair-phase1`。

## 已验证的能力

输入 GitHub Actions run URL 和 job，即可收集来源、固定 attempt/SHA、生成
验收条件、进行两次隔离复现。仅诊断模式不调用模型；修复模式接入原有
Planner、QA、Developer、Reviewer，输出 patch 和本地验证报告。

当前入口、启动命令和支持边界见 [使用指南](CI_REPAIR_GUIDE.md)。

## 本地检查

| 检查 | 结果 |
| --- | --- |
| `ruff format --check .` / `ruff check .` | 通过 |
| `mypy src` | 通过 |
| `lint-imports` | 5 项分层约束通过 |
| 已提交的 unit + CI sandbox budget contract 测试 | 184 passed |
| 原工作流回归，排除 worker restart 用例 | 20 passed，1 deselected |
| CI 固定来源 E2E：diagnose / repair | 2 passed |
| 可选真实模型 E2E，最终代码与配置 | 1 passed |

184 项检查包含新的 28 项 CI 单元测试、2 项 Temporal deadline/cancel 测试、
2 项 PostgreSQL 并发预算测试。路径根目录检索也验证了：只有获得完整仓库
读取权限时允许 `.`；受限读取和 `../outside` 仍被拒绝。

复核命令：

```powershell
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run lint-imports
uv run pytest tests/unit tests/contract/test_ci_sandbox_budget.py -q
uv run pytest tests/integration/test_code_repair_workflow.py -q -k 'not worker_restart'
uv run pytest tests/e2e/test_ci_import_pipeline.py -q
```

本次工作区另有用户未提交的 `test_e2e_scenario_catalog.py`，最终 184 项计数
使用 `--ignore=tests/unit/test_e2e_scenario_catalog.py` 排除该文件。包含它时为
188 passed；该文件及其他原有修改未纳入本次提交。

完整工作流回归首次执行在已有的 `test_worker_restart_recovers_pending_run`
未结束，等待后终止了本次测试进程。该用例未验证通过。未执行整个仓库的
所有 contract、integration 和 nightly security 测试，不将以上结果描述为全套通过。

## 真实 GitHub 来源检查：正确停止

来源：[AcodingAgent 失败运行](https://github.com/Arrowsinknees12138/AcodingAgent/actions/runs/34849717091/attempts/1)，
job `103994276916`。成功读取 GitHub API、下载并脱敏日志，识别 Python 3.12、
Ubuntu 和 checkout SHA `bbf4b3a0877c9d826ea36397e5842839df52ad72`。

该 job 在质量门禁阶段失败，日志没有受支持的 pytest FAILED node ID 与异常签名。
收集器输出 `insufficient_evidence`、`repair_eligible=false`，没有调用模型。
本次顺带修复了原提交中两个测试文件的格式问题，作为独立提交保存。

复核摘要保存在本地忽略目录 `.repopilot/ci-phase1-github-probe.json`。

## 真实模型演示：完整修复

这是本地构造的 Git 仓库与录制 CI 来源，GitHub API 使用固定适配器；
Git、PostgreSQL、MinIO、Docker 和 Temporal 均真实运行。真实模型未获得
预设补丁。它根据原失败测试与源码修复字符串处理函数错误的小写转换。

最终执行使用现有 `deepseek-flash` 配置、`json_object`，临时环境设置
`REPOPILOT_MODEL_REASONING_EFFORT=none`；用户 `.env` 未修改。

| 指标 | 最终执行 |
| --- | --- |
| Run ID | `47c3eef5-1732-4a6b-8e8a-909263ab9131` |
| 基线 SHA | `a99409e01d28722b971fe51d8c9131881bbe5c2b` |
| 候选 SHA | `ab49da9bb36050210b6ad6ffc7ec085feac18226` |
| 状态 | succeeded |
| 修改文件 | formatter.py |
| 原测试修改 | 无 |
| 模型调用 | 17 |
| 输入 / 输出 token | 71,048 / 1,433 |
| 费用 | $0.03315268，按当前配置费率计量 |
| 沙箱累计时间 | 19 秒 |
| 工作流报告时长 | 51 秒；整项测试约 66 秒 |
| 本地候选验证 | 通过，且 Reviewer 批准 |
| 远端 CI | not_verified |

修复前目标测试失败；两个新沙箱的结果与原失败签名一致。修复后目标和
原通过测试通过，测试集合与冻结文件保持一致，额外 QA 测试也通过。

最终报告与模型输出保存在本地忽略目录：
`.repopilot/ci-phase1-real-report.json`、
`.repopilot/ci-phase1-real-model-outputs.json`。
测试容器结束后销毁，不依赖其临时 MinIO URL 作为长期下载入口。

调试过程中还有 7 次未完整成功的执行：发现并修复了 synthetic tool 消息
缺少 `tool_call_id`、输出格式反馈不足、推理 token 耗尽、pytest 平铺项目
导入路径问题和 Reviewer 根目录检索问题；也观察到工具参数错误及步骤超限。
这些执行期间代码与配置不同，不能把最终一例通过换算成稳定成功率。
批量真实故障评测尚未完成。

## 本轮未交付的后续业务

仓库自动监听、GitHub App/Webhook、JUnit Artifact 导入、PR merge SHA 复现、
自动草稿 PR、远端 CI 回传和多语言/多平台环境仍属于后续阶段。当前诊断是
有证据的复现分类，尚未新增面向仅诊断模式的 LLM 根因分析报告。

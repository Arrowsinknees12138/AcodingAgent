# 第一阶段：导入 CI、诊断、候选修复

本轮测试、真实来源检查、模型演示及未验证项见 [验证记录](CI_REPAIR_VALIDATION.md)。

这份指南描述已经实现的入口。GitHub App 自动监听、自动 PR、远端 CI
结果回传属于后续阶段。原手写任务 API/CLI 继续可用。

## 启动

```powershell
uv sync
docker compose up -d postgres minio temporal temporal-ui pypi-proxy
uv run alembic upgrade head
uv run repopilot-api
# 以下命令分别在终端中运行：
uv run repopilot-worker orchestration
uv run repopilot-worker repository
uv run repopilot-worker sandbox
uv run repopilot-worker model
```

仅诊断不调用模型，因此不需要 model worker。生成候选补丁需要在 `.env`
配置原有的模型 endpoint、key 和 model name。GitHub 凭据配置为可选的
`REPOPILOT_GITHUB_TOKEN`（公开仓库的 Actions 读取权限）；它只用于 API
和 repository worker，不写入请求、证据、模型输入或沙箱。

## 使用 CLI

```powershell
# 先列出 jobs；多个失败 job 时必须选择一个
uv run repopilot ci jobs "https://github.com/owner/repo/actions/runs/123"

# 默认只诊断，无需填写 bug 或验收条件
uv run repopilot ci import "https://github.com/owner/repo/actions/runs/123" --job-id 456

# 查询进度、分类、复现结果与报告
uv run repopilot ci show <run-id>
uv run repopilot status <run-id>

# 已完成、可修复的诊断可以转为修复任务；沿用原预算和审批策略
uv run repopilot ci repair <diagnosis-run-id>

# 或第一次导入时就明确启用修复
uv run repopilot ci import "https://github.com/owner/repo/actions/runs/123" --job-id 456 --repair --max-cost 2 --max-calls 20 --wall-seconds 1800 --sandbox-seconds 1200

# 原有审批、取消、产物下载命令继续适用
uv run repopilot artifacts <run-id>
uv run repopilot cancel <run-id>
```

网页入口是 `http://127.0.0.1:8080/ui`。输入 API Token 后，在“导入失败 CI”
粘贴 run URL，读取 jobs，选择模式和预算。选择 Run 后可查看分类、两次
复现、tested SHA、本地验证与费用；“仅诊断”的可修复任务显示生成补丁按钮。
结果产物区域提供原始脱敏日志、诊断 JSON、验证结果、patch 和最终报告下载。

## API

所有接口都要求现有的 `Authorization: Bearer <REPOPILOT_API_TOKEN>`。

| 接口 | 行为 |
| --- | --- |
| `GET /v1/ci/jobs?run_url=...` | 只读获取 run attempt 和 job 列表 |
| `POST /v1/ci/import` | 固定 attempt/job，持久化请求并启动工作流，返回 202 和 Run ID |
| `GET /v1/ci/{run_id}` | 查询诊断与最终报告；进行中的结果字段可以为空 |
| `POST /v1/ci/{run_id}:repair` | 可复现诊断完成后创建 repair 模式 Run |
| `POST /v1/runs/{run_id}:cancel` | 取消并清理 |
| `GET /v1/runs/{run_id}/artifacts` | 查询产物，使用现有 Artifact download API 下载 |

导入 JSON 示例：

```json
{
  "run_url": "https://github.com/owner/repo/actions/runs/123/attempts/1",
  "job_id": 456,
  "mode": "diagnose",
  "budget": {
    "max_cost_usd": 2,
    "max_wall_time_seconds": 1800,
    "max_model_calls": 20,
    "max_sandbox_seconds": 1200
  },
  "approval_policy": {"mode": "risk_based"}
}
```

默认 mode 为 diagnose，预算为 $5 / 50 calls / 3600 wall seconds / 1800
sandbox seconds。内部键由 repo ID、run ID、attempt、job ID 和 mode 构造，
重复导入返回原任务；改变同一来源的预算/审批返回 409，不会偷偷扩大额度。
diagnose 与 repair 分别有独立预算；点击 repair 会重新收集固定来源并复现，
以防直接复用未经当前执行验证的结论。取消后重复导入同一身份不会复活。
不带 attempt 的 URL 在接收时解析为当前 attempt；重跑产生的新 attempt 是新输入。

## 执行与验收边界

1. 只支持公开 GitHub 仓库、已完成失败的 push run、托管 Ubuntu job。
2. job 日志需要证明 Python 3.12，且 `git log -1 --format=%H` 的 checkout
   SHA 与本次 run head SHA 一致。PR merge/fork/custom checkout 暂不自动处理。
3. 原始失败需包含 `FAILED tests/...py::test_name - ...` 的 pytest 汇总。
   原始 CI 没有测试 node ID、日志过期或仅有 lint/build 失败时不会猜测 bug。
4. 复现是固定的 `python -m pytest -vv --tb=short tests`，两次使用全新断网
   沙箱。首版复现整个 tests/ 集合，不支持任意原 CI shell、选定子集、服务容器、
   自定义工作目录、Windows/macOS 或 Python 版本矩阵。文本日志为当前入口；
   GitHub JUnit Artifact 导入尚未实现。
5. 只有原失败 test ID、异常及规范化消息均匹配，且两次结果一致才允许修复。
   两次一致不保证绝对没有 flaky；结果不同则停止自动修复。
6. 依赖遵循原有受控 PyPI 策略。CI 模式会安装 uv dev/test/tests groups，
   根目录 requirements-dev/test.txt，以及 pip project 的 dev/test/tests extras。
   自定义组、requirements includes、非 PyPI 来源等需要后续显式适配。
7. 原测试、conftest、依赖清单和关键配置被冻结。候选改变这些内容时，在执行前
   拒绝；目标必须变为 PASSED，原测试集合不能改变，原通过项必须仍通过。
   QA 新增密封测试独立执行，不能替代原 oracle。
8. wall deadline 包含审批等待；进入最终清理后不再因执行截止时间改写结果。
   sandbox budget 包含实际命令和依赖层首次构建时间，缓存命中无需重新构建。
   PostgreSQL 在命令开始前预留 timeout 和终止宽限，完成后结算实际时间。
   worker 中断导致未知完成状态时保留预留并报告，不能凭空释放额度。
9. 限制与取消后由现有终态清理移除标记的容器/工作区，保存部分结果。
   底层同步 Docker 操作的中断依靠容器清理与强制命令 timeout，不能保证零延迟。
10. `succeeded` 表示这次流程完成。仅诊断的成功没有 patch，
    `local_validation_passed` 为 null。候选验证通过为 true；未通过为 false。
    所有报告的 `remote_ci_status` 为 `not_verified`，不会声称远端 CI 已通过。

## 验证方式

```powershell
uv run ruff check src tests/unit/test_ci_repair.py tests/contract/test_ci_sandbox_budget.py
uv run mypy src
uv run lint-imports
uv run pytest tests/unit
uv run pytest tests/contract/test_ci_sandbox_budget.py
docker compose up -d --wait pypi-proxy
uv run pytest tests/e2e/test_ci_import_pipeline.py
```

单元测试覆盖来源校验、异常解析、环境资格、两次复现分流、原测试未修复/
跳过/删除/修改拒绝、认证和幂等键。真实 PostgreSQL 测试验证并发预算预留。
Temporal 截止时间/取消测试验证终态报告与清理。CI E2E 使用真实 Git、数据库、
MinIO、Docker 和 Temporal；GitHub 来源是由真实 pytest 输出构造的固定录制，
模型默认是确定性 FakeModelProvider，不代表真实模型成功率。

可选真实模型演示（会使用现有配置并产生模型费用）：

```powershell
$env:REPOPILOT_TEST_REAL_MODEL = '1'
$env:REPOPILOT_MODEL_REASONING_EFFORT = 'none' # 仅在供应商支持此参数时设置
uv run pytest 'tests/e2e/test_ci_import_pipeline.py::test_ci_import_real_reproduction_and_repair[repair]' --junitxml=.repopilot/ci-real-results.xml
Remove-Item Env:REPOPILOT_TEST_REAL_MODEL
Remove-Item Env:REPOPILOT_MODEL_REASONING_EFFORT
```

真实演示仍使用固定的 CI 来源，模型看不到预设修复补丁。结果报告写入忽略目录
`.repopilot/ci-phase1-real-report.json`。演示只证明这一例的流程与修复行为，
大规模真实故障评测、成功率与同条件基线比较仍需独立完成。

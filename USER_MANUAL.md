# RepoPilot 中文使用手册（Phase 0）

本手册对应当前仓库的 Phase 0 实现。RepoPilot 接受一个公开 GitHub Python 仓库和修复要求，在隔离环境中生成补丁、运行测试、评审，并保存最终报告。它**不会**替你创建 PR、合并代码或向目标仓库推送；也不应被当作已完成生产验收的托管服务。

## 1. 使用前准备

需要 Python 3.12、[uv](https://docs.astral.sh/uv/)、Git 和正在运行的 Docker Engine。目标仓库必须是公开的，地址格式为 `https://github.com/OWNER/REPO`；Phase 0 只支持 Python 项目。

在仓库根目录执行（PowerShell）：

```powershell
Copy-Item .env.example .env
uv sync --frozen
docker compose up -d --wait postgres minio temporal temporal-ui pypi-proxy
uv run alembic upgrade head
```

macOS/Linux 把第一行换成 `cp .env.example .env`。`.env` 已被 Git 忽略，不要把令牌、模型密钥或仓库源码粘贴进公开 issue、日志或提交。`docker compose` 服务使用开发环境的本地凭据；非个人开发机上应先更换 `.env` 与 Compose 中的默认凭据。

编辑 `.env` 中的模型设置：

| 配置 | 填写方式 |
| --- | --- |
| `REPOPILOT_MODEL_BASE_URL` | OpenAI-compatible API 的基础 URL，例如以 `/v1` 结尾；程序会追加 `/chat/completions`，不要在这里写完整的 chat completions 地址。 |
| `REPOPILOT_MODEL_API_KEY` | 该服务的 API Key。 |
| `REPOPILOT_MODEL_NAME` | 服务实际接受的模型 ID。 |
| `REPOPILOT_MODEL_STRUCTURED_OUTPUT_MODE` | 默认 `json_schema`；兼容端点不支持时可尝试 `json_object`。 |
| `REPOPILOT_MODEL_INPUT_USD_PER_MILLION_TOKENS`、`REPOPILOT_MODEL_OUTPUT_USD_PER_MILLION_TOKENS` | 分别填供应商公布的输入/输出每百万 token 美元价格。默认 `0` **不代表免费**，只会让报告中的估算费用显示为零。 |
| `REPOPILOT_MODEL_RESERVATION_USD` | 每次模型调用预留的预算，默认 `0.10` 美元；这不是 token 单价。 |

`REPOPILOT_MODEL_BASE_URL`、模型名和 Key 是三项不同配置。若只想先检查兼容性，可运行 `uv run repopilot-model-smoke`；它会实际发出一次可能计费的模型请求，不会执行完整修复。完整任务还需要模型按严格 JSON 结构返回 Planner、QA、Developer 和 Reviewer 的结果，连接测试通过不等于完整任务一定成功。

## 2. 启动服务

在五个独立终端、同一个仓库目录中启动：

```powershell
uv run repopilot-api
uv run repopilot-worker orchestration
uv run repopilot-worker repository
uv run repopilot-worker sandbox
uv run repopilot-worker model
```

`model` Worker 必须能读到上述模型配置；`sandbox` Worker 需要 Docker Engine。保持这些终端运行。然后检查 API：

```powershell
uv run repopilot health
Invoke-RestMethod http://127.0.0.1:8080/health/ready
```

`/health/live` 只表示 API 进程存活；`/health/ready` 当前检查数据库，不保证所有 Worker、模型端点或 Docker 都可用。

## 3. 创建一个修复任务（推荐：REST API）

推荐明确给出验收条件，否则系统会先停在需求审批。下面是 PowerShell 示例；把仓库地址、要求和条件改成自己的内容。API token 要与 `.env` 中的 `REPOPILOT_API_TOKEN` 相同，输入时不回显。

```powershell
$secureToken = Read-Host 'REPOPILOT_API_TOKEN' -AsSecureString
$apiToken = [System.Net.NetworkCredential]::new('', $secureToken).Password
$auth = @{ Authorization = "Bearer $apiToken" }
$createHeaders = @{ Authorization = "Bearer $apiToken"; 'Idempotency-Key' = ([guid]::NewGuid()).ToString() }

$body = @{
    repository = @{ url = 'https://github.com/OWNER/REPO'; revision = $null }
    requirement = '修复 add(a, b) 的加法行为；保持现有公开 API 不变。'
    acceptance_criteria = @('add(1, 2) 返回 3', '现有测试不得出现新增失败')
    budget = @{
        max_cost_usd = 5
        max_wall_time_seconds = 3600
        max_model_calls = 50
        max_sandbox_seconds = 1800
    }
    risk_level = 'low'
} | ConvertTo-Json -Depth 10

$bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
$run = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8080/v1/runs' `
    -Headers $createHeaders -ContentType 'application/json; charset=utf-8' -Body $bodyBytes
$runId = $run.run_id
$run
```

`Idempotency-Key` 必须是 UUID。同一个键重复发送**相同请求**会复用 Run；键相同而请求内容不同会返回 `409`。网络超时后重试时，请保留原键，不要立即生成新键。`revision` 建议填确定的 commit SHA 以便复现；不填时解析仓库当前 HEAD。

PowerShell 中发送包含中文的 JSON 时，要按上例显式转换成 UTF-8 字节；否则某些环境会把中文替换成 `?`，任务与验收条件会在创建时就损坏。可以下载 `create_run_request` Artifact 核对服务器收到的原文。

预算字段分别声明费用、墙钟时间、模型调用次数和沙箱时间的上限。当前模型费用预留/结算及调用次数上限已接入；实际费用估算依赖上文填写的 token 单价，单价保留 `0` 时不能把费用预算当成真实账单上限。`max_wall_time_seconds` 和 `max_sandbox_seconds` 目前只做请求校验，**尚未作为跨阶段全局截止/累计用量闸口执行**。最终报告也尚未跨 Activity 精确计量沙箱秒数（会附 warning）；不要把 `sandbox_seconds: 0` 理解为实际未使用沙箱。请通过外部监控限制长时间运行的任务。

### CLI 快速创建的限制

也可将任务写入 UTF-8 文件并执行：

```powershell
uv run repopilot run --repo https://github.com/OWNER/REPO --task .\task.md
```

当前 CLI 固定使用 5 美元、3600 秒、50 次模型调用、1800 沙箱秒的预算，且发送 `acceptance_criteria: null`。因此它会进入 `waiting_requirements_approval`；CLI 的 `approve` 目前不能填写必需的 `replacement_acceptance_criteria`。若要直接运行，优先使用上面的 REST 请求；若已用 CLI 创建，按下一节的 REST 示例补交验收条件。中/高风险、逐阶段自定义审批和依赖例外也要通过 REST 请求设置。

## 4. 查看进度与处理审批

```powershell
uv run repopilot status $runId
uv run repopilot events $runId
uv run repopilot artifacts $runId
```

也可用 `$auth` 请求 `GET /v1/runs/{run_id}`、`GET /v1/runs/{run_id}/events` 和 `GET /v1/runs/{run_id}/artifacts`。创建响应是 `202 Accepted`，只表示 Run 已提交，不表示已修复成功。终态为 `succeeded`、`failed`、`rejected` 或 `cancelled`。

常见流程如下；括号中的审批只在对应条件下出现：

```text
创建 → 解析仓库与需求 → (需求审批) → 基线测试 → 规划 → (计划审批)
     → QA 密封测试 → (测试豁免审批) → (执行审批) → 开发补丁
     → 候选验证 → Reviewer → [必要时重规划/修复，最多两轮]
     → (交付审批) → 清理资源 → 最终报告
```

风险策略默认是 Low 自动、Medium 需要计划审批、High 需要计划和执行审批。Planner 标出的风险可能把实际级别提高，因此不要仅根据创建请求的 `risk_level` 预判不会出现审批。即使选择自动模式，**缺少结构化验收条件**仍需需求审批；密封测试在基线上的预期不成立时也可能进入测试豁免审批。

在状态恰好为 `waiting_plan_approval`、`waiting_execution_approval` 或 `waiting_delivery_approval` 时，可使用：

```powershell
uv run repopilot approve $runId --kind plan --reason '已检查计划范围'
```

把 `plan` 换成当前状态对应的 `execution` 或 `delivery`。CLI 只支持批准；拒绝、需求审批和测试豁免请用 REST：

```powershell
$approval = @{
    kind = 'requirements'
    decision = 'approve'
    reason = '已确认验收条件'
    replacement_acceptance_criteria = @('add(1, 2) 返回 3')
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8080/v1/runs/$runId/approvals" `
    -Headers $auth -ContentType 'application/json' -Body $approval
```

只在 `waiting_requirements_approval` 状态提交上述请求。其他审批把 `kind` 换成 `plan`、`execution`、`test` 或 `delivery`，并去掉 `replacement_acceptance_criteria`。`decision` 可为 `approve` 或 `reject`；拒绝时 `reason` 不能为空。`test` 豁免批准时，须在 `reason` 写清被豁免的测试及理由，勿将其用作绕过真实 bug 的手段。审批与当前等待状态不匹配会被拒绝。当前实现没有自动审批超时，请主动监控等待状态或取消 Run。

### 自定义审批与依赖策略

在创建请求 JSON 中可增加：

```json
{
  "approval_policy": {
    "mode": "custom",
    "custom": {"plan": "manual", "execution": "automatic", "delivery": "manual"}
  },
  "dependency_policy": {
    "index_url": "https://pypi.org/simple",
    "allow_lockfile_read": true,
    "allow_cache": true,
    "allow_new_dependencies": false
  },
  "qa": {
    "level": "standard",
    "required": ["acceptance_tests", "targeted_tests", "scope_check"]
  }
}
```

这是要**并入创建请求**的字段片段，不是单独提交的请求。`automatic`/`manual` 可分别配置计划、执行、交付阶段。只允许官方 PyPI，允许读取 lockfile 和缓存；默认禁止新增依赖。仅当任务明确要求依赖变更时，才考虑把 `allow_new_dependencies` 改为 `true`。Reviewer 对验收不符、越界、回归、安全、明显错误处理、API 合约破坏、不必要依赖、绕过测试或改测试掩盖 bug 作 BLOCK；命名、可选重构、文档和轻微风格问题只作 COMMENT。

## 5. 查看结果、下载报告和补丁

Run 到达终态后，先列出 Artifact，再下载 `final_report` 和 `patch`。当前状态接口不直接内嵌最终报告。

```powershell
$items = Invoke-RestMethod -Uri "http://127.0.0.1:8080/v1/runs/$runId/artifacts" -Headers $auth
$report = $items | Where-Object kind -eq 'final_report' | Select-Object -Last 1
Invoke-WebRequest -Uri "http://127.0.0.1:8080/v1/artifacts/$($report.artifact_id)/download" `
    -Headers $auth -OutFile '.\final-report.json'
Get-Content '.\final-report.json' -Raw
```

在 `final-report.json` 中重点检查 `status`、`error`、`changed_paths`、`patch_ref`、`verification_ref`、`review_ref`、`model_calls`、`model_cost_usd`、`repair_rounds` 和 `warnings`。`patch_ref.artifact_id` 可用同一下载接口取得最终 diff；`verification_ref` 可取得真实测试与新增 regression 结果。Artifact 下载需要 API Bearer token，不要把报告或原始模型响应发到不可信渠道。成功也建议人工阅读 diff 与验证报告后，再自行决定是否应用到目标仓库。

取消运行中的任务：

```powershell
uv run repopilot cancel $runId
```

## 6. 常见问题

| 现象 | 检查方式 |
| --- | --- |
| `health` 不通 | 确认 `repopilot-api` 终端仍在运行，地址和端口与 `.env` 相同。 |
| `/health/ready` 返回 `503` | 检查 PostgreSQL 容器与 `uv run alembic upgrade head`；ready 当前只检查数据库。 |
| 创建返回 `401` | Bearer token 与 API 进程读取的 `.env` 不一致。 |
| 创建返回 `422` | 检查仓库 URL、UUID 格式的 Idempotency-Key、预算上限和 JSON 字段；接口拒绝未知字段。 |
| 创建返回 `409` | 同一 Idempotency-Key 被用于不同请求，或审批与当前状态不匹配。 |
| 长时间停在 `waiting_*_approval` | 查看 `status`/`events`，提交对应 `kind` 的审批；当前没有自动审批超时。 |
| 模型 Worker 无法启动或结构化输出失败 | 检查模型基础 URL、Key、模型 ID、`json_schema`/`json_object` 模式；可先运行可能计费的 smoke test。 |
| 依赖构建失败 | 检查 Docker、`pypi-proxy` 健康状态及到官方 PyPI 的受控出口；运行沙箱本身不联网。 |
| 报告费用为 0 | 检查每百万 token 的输入/输出单价是否仍为默认 `0`；预算预留额不是实际单价。 |

开发与验证命令见 [README.md](./README.md)；实现范围、状态机和安全约束见 [PHASE0_IMPLEMENTATION_DESIGN.md](./PHASE0_IMPLEMENTATION_DESIGN.md)。

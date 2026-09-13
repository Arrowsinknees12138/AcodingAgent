# RepoPilot（Phase 0）

一个可恢复、可验证、具备明确安全边界的本地多智能体代码修复原型。

- 生产级系统设计（为什么做、最终形态）：[PRODUCTION_SYSTEM_DESIGN_DEMO.md](./PRODUCTION_SYSTEM_DESIGN_DEMO.md)
- Phase 0 实施设计（本仓库严格遵循的代码结构 / 接口 / 状态机）：[PHASE0_IMPLEMENTATION_DESIGN.md](./PHASE0_IMPLEMENTATION_DESIGN.md)

本仓库的代码组织、依赖方向、Temporal Workflow 状态机、Tool ACL、Sandbox
安全约束等都以《Phase 0 实施设计》为唯一权威来源；如果代码与文档冲突，
以先更新文档、再改代码为准，避免架构决策散落在 commit 历史里。

## 当前进度

按照实施设计第 25 节的开发顺序（Milestone 1 ~ 8）推进，当前状态：

- [x] Milestone 1：仓库骨架与质量门禁（uv、Ruff、mypy、pytest、docker-compose、
      Settings、结构化日志、health endpoints）
- [x] Milestone 2：领域模型与存储（Pydantic Strict Model、SQLAlchemy 2 Async
      模型 + Alembic 初始迁移、MinIO Artifact Store、内容寻址幂等写入、
      读取 ACL）
- [x] Milestone 3：Temporal 最小闭环（`CodeRepairWorkflow` 状态机、
      `update_projection` Activity、Approval Update、取消、Worker 崩溃恢复、
      Query，均以真实 Temporal 测试 Server + 真实 PostgreSQL 验证）。现已把
      ingest、repository scan、baseline verification 接入独立 Task Queue，
      并支持 Low 自动、Medium 计划审批、High 计划+执行审批及逐阶段自定义策略。
- [x] Milestone 4：Repository Service（真实 git mirror clone/worktree/
      cherry-pick 集成、`git apply --check` 校验、AST 符号索引与接口 Diff、
      越权路径拒绝、patch 冲突检测；均以真实本地 git 仓库 + MinIO/
      PostgreSQL 验证）。`ingest_task` 与 `scan_repository` 已接入独立的
      repository Task Queue，并执行仓库体积/文件数限制校验。
- [ ] Milestone 5（进行中）：Sandbox 核心已完成（Docker Sandbox：非 root、
      `--network none`、只读根文件系统、`--cap-drop ALL`、CPU/内存/PID
      限制；`timeout --kill-after` 强制超时；`export_changes` 产出兼容
      `git apply` 的 diff；sealed test bundle 按需注入且不进最终 diff。
      均以真实 Docker 容器验证：网络隔离、Fork Bomb 遏制、超时、恶意
      cwd 拒绝、隐藏测试 ACL）。已补齐安全的测试命令发现、基线执行与
      BaselineReport；候选验证已支持基线差分与新增 regression 识别。
      依赖策略已固定为官方 PyPI、读取 lockfile、允许缓存、默认禁止新增依赖，
      并拒绝额外 index/Git/URL source；Build Sandbox 已通过内部 Docker 网络
      和带磁盘缓存的 Squid 白名单代理限制到 `pypi.org` 与
      `files.pythonhosted.org`。依赖层现已按 Source Archive 与安装计划内容寻址缓存，首次构建受控联网，
      后续 Developer/Verification 沙箱通过只读挂载在 `--network none` 下复用；真实 Docker 测试已验证。
- [ ] Milestone 6（进行中）：已实现 Fake/OpenAI-compatible Provider、稳定
      逻辑调用键、PostgreSQL 预算预留/结算/释放/UNKNOWN 状态、统一 Agent
      Loop、角色 Tool ACL、文件/命令参数安全校验、trajectory/model response
      Artifact 和四个 Role Prompt v1。已提供 `json_schema` / `json_object`
      兼容模式及真实端点 smoke test；model worker 已接入 Planner Activity，
      会执行预算预留、严格结构化输出、计划校验与 ChangePlan Artifact 持久化；
      QA Activity 也已能生成受限路径下的 sealed test bundle 和 TestPlan；Developer 会按 WorkItem
      精确读写范围生成补丁，Reviewer 严格执行 BLOCK/COMMENT 分类。待完成：交互式 sandbox RPC tool backend。
- [ ] Milestone 7（进行中）：已实现 ChangePlan/WorkItem 跨对象校验、环检测、
      路径唯一 Owner 校验和最多 4 路的确定性 DAG 分批调度。Planner、QA、
      Developer、候选 Verification、Reviewer 均已接入主 Workflow，并按 DAG wave 最多
      并发 4 个 Developer。各终态现会清理沙箱/工作区并生成 Cleanup/Final Report，
      包含 diff、验证、评审与模型费用引用。Verification/Reviewer 失败会在原批准
      路径内最多修复两轮，重规划按风险重新审批；授权的依赖清单变更会为候选
      重新构建受控依赖层。真实本地 Git + PostgreSQL/MinIO + Docker + Temporal 的
      Fake Model E2E 已覆盖单文件修复、验收失败后重规划修复、预算耗尽，并核验
      最终 diff、测试结果与失败报告；第 23.5 节其余场景仍待补齐。
- [ ] Milestone 8：评测与交付

控制面现已提供创建、查询、审批、取消、事件、Artifact 列表和 Artifact 下载
REST API；CLI 已提供 `run`、`status`、`approve`、`cancel`、`events`、
`artifacts`。创建 Run 使用 PostgreSQL 幂等键、内容寻址请求 Artifact 和固定
Temporal Workflow ID。

## 当前策略基线

- Low：自动执行。
- Medium：需要 Plan approval。
- High：需要 Plan approval 与 Execution approval。
- 自定义模式：可分别把 plan、execution、delivery 配置为 automatic/manual。
- QA `standard`：强制 `acceptance_tests`、`targeted_tests`、`scope_check`。
- Reviewer 仅对验收失败、越界、明显回归、安全、明显错误处理、API contract、
  不必要依赖、绕过测试、改测试掩盖 bug 做 BLOCK；命名、可选重构、docstring、
  轻微风格仅 COMMENT。

## 环境要求

- Python 3.12（推荐用 `uv python install 3.12` 管理，不依赖系统 Python 版本）
- [uv](https://docs.astral.sh/uv/)
- Docker Engine（用于本地依赖服务 + Phase 0 的不可信代码沙箱）
- Git

## 快速开始

```bash
uv sync
cp .env.example .env   # 按需修改，.env 不会被提交

docker compose up -d postgres minio temporal temporal-ui

uv run repopilot-api          # 启动 API（另开一个终端）
uv run repopilot health       # CLI 调用 /health/live 验证连通性
uv run repopilot-model-smoke  # 产生一次很小的真实模型调用，验证结构化输出
```

Build Sandbox 需要 PyPI 出口时，另启动 `pypi-proxy`。沙箱所在网络是
`internal`，不能绕过代理直连公网；代理仅允许官方 PyPI 两个域名。

```bash
docker compose up -d pypi-proxy
```

或使用封装脚本：

```bash
./scripts/dev.sh sync
./scripts/dev.sh up
./scripts/dev.sh api
```

Windows PowerShell：

```powershell
.\scripts\dev.ps1 sync
.\scripts\dev.ps1 up
.\scripts\dev.ps1 api
```

## 质量门禁

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run lint-imports          # 依赖方向：见实施设计第 6 节
uv run pytest tests/unit
```

对应 `scripts/dev.sh check`。

`tests/contract` 用 testcontainers 拉起临时 PostgreSQL/MinIO 验证 Artifact
Store 等 adapter，`tests/integration` 额外用 Temporal 的时间跳跃测试 Server
验证 `CodeRepairWorkflow`（状态机、Update、取消、Worker 崩溃恢复、Query）；
两者都需要本机 Docker Engine 可用：

```bash
uv run pytest tests/contract
uv run pytest tests/integration
```

`tests/security` 会真的创建/销毁 Docker 容器验证沙箱的安全边界（网络隔离、
Fork Bomb 遏制、超时强制终止、恶意路径拒绝、密封测试 ACL）；CI 里放在
nightly（`.github/workflows/nightly.yml`），不在每个 PR 都跑：

```bash
uv run pytest tests/security
```

`tests/e2e` 使用 Fake Model，实际运行 Git、PostgreSQL/MinIO、Docker 沙箱和
Temporal；首次运行需经受控代理从官方 PyPI 安装 pytest，之后复用本地依赖层：

```bash
docker compose up -d --wait pypi-proxy
uv run pytest tests/e2e
```

启动 orchestration Worker（连接 `docker compose up -d postgres temporal`
之后）：

```bash
uv run repopilot-worker orchestration
uv run repopilot-worker repository
uv run repopilot-worker sandbox
uv run repopilot-worker model
```

## 数据库迁移

```bash
docker compose up -d postgres
uv run alembic upgrade head
```

新增/修改 SQLAlchemy 模型后：

```bash
uv run alembic revision --autogenerate -m "描述这次变更"
```

## 已知限制（Phase 0）

以下限制是 Phase 0 的**明确不实现范围**（见实施设计 3.2 节），不是遗漏：

- 不创建/修改真实 GitHub Pull Request，不支持私有仓库。
- 不支持 Kubernetes / gVisor / microVM，隔离仅依赖 Docker Engine 的
  资源限制与能力裁剪。
- 不实现多租户对外服务（数据库保留 `tenant_id` 字段，本地固定为
  `local` 租户）。
- 不实现 Web UI，只有 REST API 和 CLI。
- 不支持 Python 以外的目标语言。
- 不实现向量数据库 / RAG，代码检索使用 AST + ripgrep + 受控文件读取。
- 不自动合并或推送远程仓库；最终产物是本地候选分支 + patch + JSON 报告。

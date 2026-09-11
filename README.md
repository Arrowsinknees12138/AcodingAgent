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
      Query，均以真实 Temporal 测试 Server + 真实 PostgreSQL 验证）
- [x] Milestone 4：Repository Service（真实 git mirror clone/worktree/
      cherry-pick 集成、`git apply --check` 校验、AST 符号索引与接口 Diff、
      越权路径拒绝、patch 冲突检测；均以真实本地 git 仓库 + MinIO/
      PostgreSQL 验证）。**尚未接入 Temporal Activity**——`activities/
      repository.py`、`ingest_task`/`scan_repository` 的仓库体积/文件数
      限制校验，留到 Milestone 7 真正替换掉 Workflow 里的占位状态跳转时
      再做，避免在还没有调用方的情况下先搭一层 Activity 包装。
- [x] Milestone 5：Sandbox 与 Verification（Docker Sandbox：非 root、
      `--network none`、只读根文件系统、`--cap-drop ALL`、CPU/内存/PID
      限制；`timeout --kill-after` 强制超时；`export_changes` 产出兼容
      `git apply` 的 diff；sealed test bundle 按需注入且不进最终 diff。
      均以真实 Docker 容器验证：网络隔离、Fork Bomb 遏制、超时、恶意
      cwd 拒绝、隐藏测试 ACL）。同样**尚未接入 Temporal Activity**，
      理由同 Milestone 4。
- [ ] Milestone 6：Model Gateway 与 Agents
- [ ] Milestone 7：DAG、Repair 与完整 E2E
- [ ] Milestone 8：评测与交付

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

启动 orchestration Worker（连接 `docker compose up -d postgres temporal`
之后）：

```bash
uv run repopilot-worker orchestration
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

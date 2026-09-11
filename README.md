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
- [ ] Milestone 2：领域模型与存储
- [ ] Milestone 3：Temporal 最小闭环
- [ ] Milestone 4：Repository Service
- [ ] Milestone 5：Sandbox 与 Verification
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
uv run pytest tests/unit
```

对应 `scripts/dev.sh check`。

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

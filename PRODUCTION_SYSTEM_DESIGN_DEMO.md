# RepoPilot：生产级多智能体代码工程平台设计文档（Demo）

> 文档状态：Draft / Demo  
> 目标读者：后端工程师、AI Infra 工程师、SRE、安全评审人员、项目面试官  
> 版本：v0.4  
> 最后更新：2026-09-12
> 配套实施文档：[RepoPilot Phase 0 实施设计](./PHASE0_IMPLEMENTATION_DESIGN.md)

## 1. 文档目的

本文描述一个面向真实代码仓库的多智能体软件工程平台。平台接收 Issue、需求说明或修复任务，在隔离环境中完成需求分析、代码修改、静态检查、测试、评审和交付候选变更。

本文重点不是 Agent 角色数量，而是让整个执行过程具备以下生产属性：

- 可恢复：进程或节点故障后能够从检查点继续执行。
- 可审计：每个模型调用、工具调用、文件变更和决策都有记录。
- 可隔离：生成代码不能直接在控制平面或宿主机执行。
- 可控制：限制模型费用、运行时长、并发和资源使用。
- 可验证：交付结果必须通过确定性质量门禁，而非只依赖 LLM 判断。
- 可演进：模型、沙箱、调度器和 Agent 策略可以独立替换。

## 2. 背景与问题定义

单 Agent 编程系统实现简单，但在较大任务上通常面临：

1. 上下文快速膨胀，规划、编码和测试信息相互污染。
2. 一个 Agent 同时承担实现与评审，容易产生确认偏差。
3. 长任务发生超时或进程退出后，只能从头执行。
4. 多文件任务缺少显式依赖，容易产生接口不一致。
5. LLM 生成的命令和代码具有不可信输入属性，直接执行风险过高。
6. 缺少统一评测，无法证明多 Agent 相比单 Agent是否更好。

RepoPilot 将任务建模为可持久化的工作流，由不同能力单元协作完成，但所有 Agent 都必须服从同一套权限、预算和质量门禁。

## 3. 目标与非目标

### 3.1 目标

- 支持 Python 仓库的缺陷修复、小型功能开发和测试补全。
- 固定使用 Planner、Developer、QA、Reviewer 四种 Agent Role；Architect 合并进 Planner，Repairer 作为 Developer 的任务模式，不新增 Role。
- 支持文件级依赖 DAG、并发调度和失败后的最小范围重试。
- 支持至少一次任务执行语义，并通过幂等键避免重复副作用。
- 支持 Git worktree 级别的 Agent 工作空间隔离。
- 所有不可信代码均在短生命周期沙箱中运行。
- 提供 Token、费用、延迟、成功率及资源使用情况的可观测性。
- 支持离线评测，并可与单 Agent 基线进行可复现实验。

### 3.2 非目标

- v1 不自动合并到受保护分支，只生成 Pull Request 候选。
- v1 不支持生产环境部署和基础设施变更。
- v1 不承诺自主完成任意规模的架构重写。
- v1 不让 Agent 直接访问生产密钥、生产数据库或内网管理面。
- v1 不以完全取代人工 Code Review 为目标。
- v1 只承诺单区域多可用区高可用，不承诺跨区域灾备。

## 4. 成功指标与 SLO

### 4.1 产品指标

| 指标 | v1 目标 | 说明 |
|---|---:|---|
| Valid Patch Rate | ≥ 60% | 在固定评测集中，补丁可应用、仓库可构建且任务未超时的 Run 数 / 全部 Run 数；超时计失败 |
| Test Pass Rate | ≥ 50% | 在固定评测集中，独立验证沙箱通过目标测试的 Run 数 / 全部 Run 数；每个任务固定运行 3 次 |
| Task Regression Rate | ≤ 5% | 至少出现一个原有通过测试回归的 Run 数 / 产生候选补丁的 Run 数；同时附测试用例级回归率 |
| Human Acceptance Rate | ≥ 40% | Pilot 期间所有进入 Delivery Approval 的候选 PR 中，被人工接受或仅作非功能修改后接受的数量占比；禁止选择性送审 |
| 单任务成本 P95 | 可配置 | 默认不超过租户配置的硬预算 |

Phase 0 使用 20 个任务做冒烟基线；v1 发布评测集不少于 100 个按单文件、跨文件、并发/事务、需求歧义和安全风险分层的 Python GitHub Issue 修复任务。实验锁定仓库 revision、模型、Prompt、温度和预算，每个任务运行 3 次，报告均值、标准差、Bootstrap 95% 置信区间、失败原因分布和单 Agent 基线。阈值只允许通过版本化 ADR 调整。

### 4.2 服务 SLO

- 控制平面 API 月可用性：99.9%。
- 已接收任务不丢失：返回 `202` 的请求中，能够通过 `run_id` 查询到对应 Temporal Workflow 或明确终态的比例 ≥ 99.99%；启动 Workflow 失败时不得返回 `202`。
- 工作流状态查询延迟 P95：小于 500 ms。
- 取消请求生效时间 P95：小于 10 秒；定义为 Temporal 接受 Workflow Cancellation Request 到 Sandbox Service 向容器/Kubernetes Job 发出终止的时间，不等同于所有子进程已经退出。
- 审计事件持久化延迟 P95：小于 5 秒。

模型供应商不可用不计入平台 API 可用性，但必须以明确状态暴露，不得伪装成任务成功。

## 5. 核心设计原则

1. **控制平面与执行平面分离**：编排服务不执行生成代码。
2. **LLM 输出始终不可信**：结构化解析、权限校验和沙箱边界缺一不可。
3. **先记录执行意图，再产生副作用**：Temporal 先持久化 Workflow 决策，再调度幂等 Activity；外部副作用的结果以 Artifact 和业务账本记录。
4. **确定性检查优先于模型判断**：编译、lint、类型检查、测试先执行，Reviewer 再补充语义检查。
5. **能力授权而非角色信任**：Agent 名叫 Reviewer 并不意味着拥有更多系统权限。
6. **事件可重放，副作用需幂等**：Worker 重试不能重复创建分支、重复扣费或重复提交。
7. **基线优先**：任何多 Agent 优化都必须和更简单的单 Agent 方案比较。

## 6. 总体架构

```text
                         ┌────────────────────┐
                         │ Web / CLI / CI App │
                         └─────────┬──────────┘
                                   │ REST / Webhook
                         ┌─────────▼──────────┐
                         │ API Gateway + Auth │
                         └─────────┬──────────┘
                                   │
                 ┌─────────────────▼─────────────────┐
                 │ Workflow Control Plane            │
                 │ State Machine / Budget / Policy   │
                 └───────┬───────────────┬───────────┘
                         │ tasks         │ projections
                  ┌──────▼──────┐   ┌────▼──────────┐
                  │ Temporal    │   │ PostgreSQL    │
                  │ Task Queues │   │ + Object Store│
                  └──────┬──────┘   └───────────────┘
                         │          └───────────────┘
        ┌────────────────▼────────────────────────────┐
        │ Agent Workers                               │
        │ Planner / Developer / Reviewer / QA         │
        └───────────┬─────────────────┬───────────────┘
                    │ model calls     │ sandbox jobs
             ┌──────▼───────┐   ┌────▼────────────────┐
             │ Model Gateway│   │ Execution Plane      │
             │ quota/cache  │   │ Ephemeral Sandboxes │
             └──────────────┘   └────┬────────────────┘
                                      │
                              ┌───────▼────────┐
                              │ Git Provider   │
                              │ PR / Checks    │
                              └────────────────┘
```

### 6.1 控制平面

控制平面负责身份认证、状态机推进、预算控制、策略判断和任务取消，不直接挂载用户仓库，也不执行 Shell 命令。

### 6.2 执行平面

执行平面由短生命周期沙箱组成。每个并发 `WorkItem` 使用独立可写文件系统和独立沙箱，不同 WorkItem 不共享可写目录；可信 Repository Service 管理 worktree，并只向沙箱导出不含 `.git` 的源码快照。开发环境使用 rootless Docker；staging 和 production 使用 Kubernetes Job，并通过 `RuntimeClass=runsc` 运行在 gVisor 中。验证阶段必须新建一个未复用 Developer 文件系统的沙箱。v1 不支持 microVM；超过 gVisor 风险边界的任务直接由策略引擎拒绝。沙箱结束后只保留声明过的 Artifact，文件系统整体销毁。

### 6.3 模型网关

模型网关统一处理供应商适配、超时、重试、速率限制、Token 统计、内容脱敏和请求追踪。Agent 不直接依赖某个厂商 SDK。

缓存分两层，任何缓存都只影响性能，不参与正确性判断：

- **Provider 侧 Prompt Caching**：仅在供应商明确支持时复用稳定的系统 Prompt 和仓库前缀。其命中规则由供应商决定，RepoPilot 不把它描述为可控幂等缓存，也不假设客户端可以指定 cache key。
- **网关侧响应缓存**：只对无副作用的信息抽取和简单分类启用。缓存键为 `hash(tenant_id, provider, model, model_parameters, prompt_version, tool_schema_version, policy_version, base_revision, input_artifact_hashes)`，默认 TTL 15 分钟且不跨 Run；代码补丁、测试生成和 ReviewDecision 禁用普通响应缓存，只使用 8 章定义的逻辑调用结果去重。

### 6.4 已确定的技术基线

- API：FastAPI。
- 顶层工作流：Temporal Python SDK；不同时使用 LangGraph 管理 Run 状态。
- 业务元数据：PostgreSQL + SQLAlchemy 2 + Alembic。
- Artifact：兼容 S3 的对象存储；本地开发使用 MinIO。
- 数据契约：Pydantic v2，`strict=True`、`extra="forbid"`。
- 执行隔离：开发使用 rootless Docker，生产使用 Kubernetes Job + gVisor。
- 可观测性：OpenTelemetry。
- 首个产品场景：Python 公共 GitHub 仓库的 Issue 修复；SWE-bench 仅作为离线评测集，不作为产品入口。

Temporal Event History 是 Run 编排状态和故障恢复的唯一真实来源。业务 PostgreSQL 保存面向 API 查询的 Run 投影、租户、权限、Artifact 元数据和用量账本，不另行实现一套可推进工作流的业务状态机。

## 7. 领域模型

```python
class RunStatus(str, Enum):
    QUEUED = "queued"
    WAITING_REQUIREMENTS_APPROVAL = "waiting_requirements_approval"
    PLANNING = "planning"
    WAITING_PLAN_APPROVAL = "waiting_plan_approval"
    DESIGNING_TESTS = "designing_tests"
    WAITING_TEST_APPROVAL = "waiting_test_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    REVIEWING = "reviewing"
    REPLANNING = "replanning"
    DELIVERING = "delivering"
    WAITING_DELIVERY_APPROVAL = "waiting_delivery_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class WorkItemStatus(str, Enum):
    BLOCKED = "blocked"
    READY = "ready"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskSpec(BaseModel):
    task_id: UUID
    tenant_id: UUID
    repository: RepositoryRef
    base_revision: str  # Ingest 阶段从 repository.base_revision 固定的不可变版本，见 7.1
    requirement: str = Field(min_length=1, max_length=200_000)
    acceptance_criteria: list[str] = Field(min_length=1)
    acceptance_criteria_source: Literal["structured", "heuristic"]
    policy_profile: str
    budget: Budget


class WorkItem(BaseModel):
    work_item_id: UUID
    run_id: UUID
    kind: Literal["plan", "code", "test", "review", "repair"]
    dependencies: list[UUID]
    allowed_paths: list[str]
    required_capabilities: list[str]
    attempt: int
    idempotency_key: str
```

关键实体：

- `Task`：用户提交的稳定需求。
- `Run`：一次具体执行，可因模型、配置或基线版本不同而重复创建。
- `WorkItem`：最小调度单元。
- `Artifact`：计划、补丁、日志、测试报告、评审结果等不可变产物。
- `Checkpoint`（只读投影，非独立恢复机制）：Activity 在关键节点把 Temporal Event History 中的进度（当前状态、已完成 WorkItem、已消耗预算）写入 PostgreSQL，仅供 API 查询和人工排障使用；恢复语义完全由 Temporal Event History 决定，业务层不维护第二套可写的恢复状态。
- `PolicyDecision`：权限或安全策略的审计记录。
- `UsageLedger`：模型费用和计算资源使用账本。

数据库保存元数据和状态；大日志、补丁包、测试报告保存到对象存储。Artifact 使用 SHA-256 内容寻址，数据库只保存摘要、位置和所有者。

### 7.1 关键 Artifact 结构

上文各 Role/Service 的输入输出（`ChangePlan`、`PatchProposal`、`IntegratedPatchArtifact`、`TestPlan`、`VerificationReport`、`ReviewDecision`）此前只出现过类型名，这里补齐字段定义，避免“结构化输出”停留在口号层面：

```python
class ArtifactKind(str, Enum):
    REPOSITORY_SNAPSHOT = "repository_snapshot"
    CHANGE_PLAN = "change_plan"
    SOURCE_ARCHIVE = "source_archive"
    PATCH = "patch"
    EXPORTED_INTERFACE = "exported_interface"
    TEST_BUNDLE = "test_bundle"
    VERIFICATION_REPORT = "verification_report"
    REVIEW_DECISION = "review_decision"
    LOG = "log"


class ArtifactRef(BaseModel):
    artifact_id: UUID
    run_id: UUID
    tenant_id: UUID
    kind: ArtifactKind
    schema_version: str
    uri: str
    sha256: str
    size_bytes: int
    base_revision: str
    input_artifact_ids: list[UUID]
    created_at: datetime


class RepositoryRef(BaseModel):
    provider: Literal["github"]
    owner: str
    name: str
    default_branch: str | None = None  # 客户端可选提供；缺省时 Ingest 查询 GitHub API 解析
    base_revision: str | None = None  # 客户端可选提供；缺省时 Ingest 解析 default_branch 的 HEAD 并回填到 TaskSpec.base_revision


class Budget(BaseModel):
    max_cost_usd: float
    max_wall_time_seconds: int
    max_model_calls: int
    max_sandbox_seconds: int


class RepositorySnapshot(BaseModel):
    base_revision: str
    primary_language: str
    dependency_manifest_paths: list[str]
    test_entry_points: list[str]
    forbidden_paths: list[str]
    index_ref: str  # Repository Service 索引句柄，见 9.3


class PlannedFileChange(BaseModel):
    work_item_id: UUID
    path: str
    owner: str
    operation: Literal["create", "modify", "delete"]
    responsibility: str
    required_interfaces: list[str]  # Planner 提出的接口约束，不冒充代码中已存在的真实接口


class ChangePlan(BaseModel):
    plan_id: UUID
    version: int
    supersedes_plan_id: UUID | None
    files: list[PlannedFileChange]
    dependency_edges: list[tuple[UUID, UUID]]  # (upstream_work_item_id, downstream_work_item_id)
    risk_flags: list[
        Literal[
            "dependency_change",
            "auth_change",
            "migration",
            "ci_config",
            "large_scope",
            "unclear_acceptance_criteria",
        ]
    ]


class PatchProposal(BaseModel):
    work_item_id: UUID
    attempt: int
    input_revision: str
    patch_ref: ArtifactRef  # unified diff 正文存对象存储，不进入 Temporal Event History
    touched_paths: list[str]


class IntegratedPatchArtifact(BaseModel):
    work_item_id: UUID
    proposal_ref: ArtifactRef
    input_revision: str
    commit_sha: str
    exported_interface_ref: ArtifactRef


class TestPlan(BaseModel):
    test_plan_id: UUID
    origin: Literal["sealed", "post_patch"]
    test_bundle_ref: ArtifactRef
    test_cases: list["TestCaseSpec"]
    acceptance_criteria_mapping: dict[str, list[str]]  # acceptance_criteria -> test 用例名


class TestCaseSpec(BaseModel):
    name: str
    purpose: Literal["bug_reproduction", "acceptance", "regression"]
    expected_on_base: Literal["pass", "fail", "skip", "not_applicable"]
    expected_on_candidate: Literal["pass"] = "pass"


class ExportedSymbol(BaseModel):
    qualified_name: str
    kind: Literal["function", "class", "constant"]
    signature: str
    change: Literal["added", "modified", "removed", "unchanged"]
    confidence: float


class ExportedInterfacePayload(BaseModel):
    work_item_id: UUID
    commit_sha: str
    symbols: list[ExportedSymbol]
    unresolved_references: list[str]


class DeveloperContext(BaseModel):
    work_item_id: UUID
    input_revision: str
    source_snapshot_ref: ArtifactRef
    upstream_interface_refs: list[ArtifactRef]
    read_paths: list[str]
    allowed_write_paths: list[str]


class VerificationFinding(BaseModel):
    file_path: str
    severity: Literal["blocker", "major", "minor"]
    message: str


class VerificationReport(BaseModel):
    work_item_id: UUID | None  # None 表示合并后的全量验证
    passed: bool
    findings: list[VerificationFinding]
    test_pass_count: int
    test_fail_count: int
    regression_count: int
    report_ref: ArtifactRef
    stdout_ref: ArtifactRef | None


class ReviewFinding(BaseModel):
    finding_id: UUID
    file_path: str | None
    severity: Literal["blocker", "major", "minor"]
    category: Literal["correctness", "security", "compatibility", "maintainability"]
    message: str
    suggested_paths: list[str]


class ReviewDecision(BaseModel):
    decision: Literal["approve", "request_changes", "reject"]
    rationale: str
    findings: list[ReviewFinding]
```

所有 Role 和 Activity 的大型输入输出都先写入 Artifact Store，再把 `ArtifactRef` 传回 Workflow。Temporal Event History 中禁止保存源码、完整 diff、完整测试源码和原始命令输出。Artifact Store 按 `tenant_id/run_id/kind/sha256` 内容寻址并采用条件写；只有同一租户、同一 Run 的幂等重试返回已有对象，不做跨租户内容去重。

`ArtifactRef.uri` 是对象存储内部 key，不是长期签名 URL。Artifact Service 根据 `tenant_id + run_id + kind + caller_role` 发放最长 5 分钟的下载凭证：Developer 无权读取 `kind="test_bundle"`，只有 QA、Verification Service 和获批人工 Reviewer 可读；源码快照只授权给对应 Sandbox Service 实例。每次授权和下载均写入 `audit_events`。

`ChangePlan.dependency_edges` 与 `WorkItem.dependencies` 保持一致来源：Planner 只产出前者，Dependency Scheduler 据此生成后者，两者不允许出现不一致（见 9.3 校验规则）。`RepositorySnapshot` 由 Repository Scan（8.1 step 2）产出，是 Planner 除 `TaskSpec` 外的另一项输入。`RepositoryRef.default_branch` 与 `base_revision` 都允许客户端省略、由 Ingest 解析，但两者用途不同：`base_revision` 是本次 Run 的执行基线，解析后固定进 `TaskSpec.base_revision` 且全程不变；`default_branch` 只是仓库元数据。每个 Artifact 的 `base_revision` 表示最初执行基线，`PatchProposal.input_revision` 则表示该 WorkItem 实际读取的集成分支版本。`PatchProposal` 与 `IntegratedPatchArtifact` 分开且均不可变，Repository Service 不对 Developer 产物进行“回填修改”。

## 8. 工作流与状态机

```text
QUEUED
  ├─→ WAITING_REQUIREMENTS_APPROVAL（验收条件为空）→ PLANNING
  └─→ PLANNING
  ├─→ WAITING_PLAN_APPROVAL（仅高风险计划）→ DESIGNING_TESTS
  └─→ DESIGNING_TESTS（普通计划）
  → WAITING_TEST_APPROVAL（仅测试设计连续失败）
  → EXECUTING
  → VERIFYING
  → REVIEWING
  ├─→ DELIVERING → WAITING_DELIVERY_APPROVAL → SUCCEEDED
  ├─→ EXECUTING（局部修复，受最大轮数限制）
  ├─→ REPLANNING → PLANNING
  ├─→ REJECTED
  ├─→ FAILED
  └─→ CANCELLED
```

`CANCELLED` 可由除 `SUCCEEDED`、`FAILED`、`REJECTED` 外的任意状态进入。Plan Approval 被拒绝、Delivery Approval 被拒绝以及 Reviewer 明确 `reject` 均进入 `REJECTED`；系统故障、预算耗尽和审批超时进入 `FAILED`。完整转换契约以 `current_state + event + guard -> next_state + activities` 表维护，并为非法转换编写单元测试。

关键转换定义如下；未列出的组合一律拒绝：

| 当前状态 | 事件与条件 | 下一状态 | 主要 Activity |
|---|---|---|---|
| `QUEUED` | 验收条件为空 | `WAITING_REQUIREMENTS_APPROVAL` | 记录审批请求 |
| `QUEUED` | 验收条件非空 | `PLANNING` | 创建计划 |
| `WAITING_REQUIREMENTS_APPROVAL` | 批准且补全条件 | `PLANNING` | 创建计划 |
| `PLANNING` | 计划有效且高风险 | `WAITING_PLAN_APPROVAL` | 记录审批请求 |
| `PLANNING` | 计划有效且普通风险 | `DESIGNING_TESTS` | 生成密封测试 |
| `PLANNING` | 命中禁止路径或策略硬拒绝 | `REJECTED` | 记录 PolicyDecision |
| `WAITING_PLAN_APPROVAL` | 批准 | `DESIGNING_TESTS` | 生成密封测试 |
| `DESIGNING_TESTS` | 测试满足基线预期 | `EXECUTING` | 调度 Ready WorkItem |
| `DESIGNING_TESTS` | 连续两次无效 | `WAITING_TEST_APPROVAL` | 等待人工修订/豁免 |
| `WAITING_TEST_APPROVAL` | 批准测试修订或显式豁免 | `EXECUTING` | 记录批准人、理由和被豁免测试 |
| `EXECUTING` | 所有 WorkItem 集成完成 | `VERIFYING` | 全量验证 |
| `VERIFYING` | 门禁通过 | `REVIEWING` | 语义评审 |
| `VERIFYING/REVIEWING` | 可局部修复且未超限 | `EXECUTING` | 创建 Repair WorkItem |
| `VERIFYING/REVIEWING` | 计划结构错误 | `REPLANNING` | 生成新版计划 |
| `REPLANNING` | 新计划生成 | `PLANNING` | 重新进行策略检查 |
| `REVIEWING` | Reviewer approve | `DELIVERING` | 推送分支并创建 Draft PR |
| `DELIVERING` | Draft PR 创建成功 | `WAITING_DELIVERY_APPROVAL` | 等待人工审批 |
| `WAITING_DELIVERY_APPROVAL` | 批准 | `SUCCEEDED` | 标记 PR Ready for Review |
| 任意等待审批状态 | 拒绝 | `REJECTED` | 审计并清理临时资源 |
| 任意非终态 | 取消 | `CANCELLED` | 传播取消并清理资源 |

Temporal Workflow 负责状态转换、计时器、Update/Signal 和取消传播。Workflow 代码不得直接访问数据库、Git、模型或沙箱；所有外部 I/O 都封装为 Activity。Activity 采用至少一次执行语义，因此 Git 提交、PR 创建、模型计费和 Artifact 写入均必须携带 `idempotency_key`，并以数据库唯一约束或外部服务幂等接口消除平台侧重复副作用。

`idempotency_key` 按副作用类型固定生成规则，Activity 重放时用相同输入即可推导出相同 key，不依赖内存状态：

| 副作用 | 生成规则 |
|---|---|
| 模型逻辑调用 | `sha256(tenant_id, work_item_id, attempt, provider, model, model_parameters, prompt_version, tool_schema_version, input_artifact_hashes, policy_version)` |
| Git 提交 / 分支创建 | `sha256(work_item_id, attempt, action_type)` |
| 候选 PR 创建或更新 | `sha256(run_id)`（同一 Run 只对应一个候选 PR，更新复用同一 key） |
| 计费扣减 | 直接使用 `model_call_id`（本身已全局唯一） |

模型供应商通常不能提供端到端 exactly-once。Model Gateway 在调用前以逻辑 `model_call_id` 创建费用预留和 `PENDING` 记录，成功后先持久化响应 Artifact 再结算实际用量；重试先查询已有成功 Artifact。若供应商已经返回但 Worker 在持久化前崩溃，该调用标记为 `UNKNOWN_COMPLETION`，代码生成等高费用调用默认不自动重发，由 Workflow 在剩余预算内创建新 attempt。文档只承诺“平台副作用幂等和费用有上限”，不承诺外部模型绝不重复计费。

本架构不额外建设独立业务队列和 Outbox 来推进 Run，避免 Temporal 与自研消息状态机出现双重真实来源。PostgreSQL 查询投影由幂等 Activity 更新；投影短暂落后不影响 Workflow 正确性。

### 8.1 标准执行流程

1. **Ingest**：先持久化外部 `CreateRunRequest`，校验仓库授权并固定 base revision。验收条件优先使用客户端显式数组，其次解析 Issue 的 Markdown checklist，最后才做启发式抽取；前两者标记 `acceptance_criteria_source="structured"`，启发式结果标记为 `"heuristic"`。只有得到非空验收条件后才创建内部 `TaskSpec`；否则保留原请求并转入 `WAITING_REQUIREMENTS_APPROVAL`，人工补全后再创建 `TaskSpec` 和进入 Planning。
2. **Repository Scan**：由 Repository Service 建立语言、依赖、测试入口、敏感路径清单和只读代码索引（见 9.3），供 Planning 和 Execution 阶段复用。
3. **Planning**：Planner 输出结构化 `ChangePlan` 和文件依赖 DAG。Dependency Scheduler 对 DAG 做拓扑排序校验；存在环时判定计划无效，直接退回 Planner 重新规划，不进入执行阶段，且该次重规划不计入第 9 步的 Repair 轮数。
4. **Policy Check / Plan Approval**：普通计划自动通过；`TaskSpec.acceptance_criteria_source="heuristic"`，或 `ChangePlan.risk_flags` 命中依赖清单、认证授权、数据库迁移、CI 配置、`unclear_acceptance_criteria`，或文件数超过 20 个，均进入人工 Plan Approval；禁止路径直接拒绝。审批超时策略见 8.3。
5. **Sealed Test Design**：QA Agent 在独立上下文中生成 `TestPlan`，测试源码以 `ArtifactRef(kind="test_bundle")` 密封保存；Developer 只能看到验收条件和测试目的摘要，看不到测试源码。确定性的 Verification Service 在 base revision 试跑测试：`regression` 应通过，`bug_reproduction` 应至少有一个稳定失败，`acceptance` 按声明的 `expected_on_base` 判定。并发等非确定性复现测试固定重复 3 次；结果不稳定则标记 `flaky`，退出自动质量门禁并进入 `WAITING_TEST_APPROVAL`。测试生成最多重试 2 次，仍不满足声明预期也进入人工审批。
6. **Execution**：Dependency Scheduler 只调度依赖已经集成完成的 Developer WorkItem。Trusted Repository Service 从对应 `input_revision` 导出不含 `.git` 的源码快照，Developer 在独立沙箱中编辑并返回 unified diff Artifact。
7. **Deterministic Verification**：Trusted Repository Service 校验并应用 patch、生成最新接口 Artifact、创建提交；随后 Verification Service 在全新沙箱中执行格式化检查、语法、lint、类型检查、原有测试和密封验收测试，并独立生成 `VerificationReport`。QA Agent 不负责宣告测试通过。
8. **Review**：独立 Reviewer 只读取需求、最终 diff 和验证证据，不继承 Developer 对话。
9. **Repair**：默认交给失败文件的原 Owner；若 `findings` 涉及多个不同 Owner 的文件（如接口不匹配导致双方文件都失败），优先修复依赖 DAG 中更上游（被依赖方）的文件，下游 Owner 的 Repair WorkItem 保持阻塞直至上游修复并重新验证通过；无法判定上下游（循环耦合）时不做局部修复，直接回到 Planner 重新规划——与 step 3 的 DAG 环检测一致，这类由计划结构问题（而非实现质量问题）触发的重新规划不计入本步的 Repair 轮数上限。同一 WorkItem 连续失败两次后停止局部重试并返回 Planner 重新规划，这类由实现质量问题触发的重新规划计入 Repair 轮数。单个 Run 最多两轮 Repair，且不得突破总预算。
10. **Delivery / Approval**：Reviewer 通过后，Trusted Repository Service 推送候选分支并创建 Draft PR，然后进入 `WAITING_DELIVERY_APPROVAL`。人工批准后把 Draft PR 标记为 Ready for Review 并将 Run 置为 `SUCCEEDED`；拒绝则关闭 Draft PR、记录原因并进入 `REJECTED`。候选分支保留 7 天后清理；若要求重新生成，需发起新 Run，不复用旧分支。v1 永不自动合并受保护分支。

Step 4 的风险判定不完全依赖 Planner 自报的 `risk_flags`：`unclear_acceptance_criteria` 来自 Ingest 阶段的确定性标记（见 step 1），文件数是否超过 20 由 Policy Engine 直接对 `ChangePlan.files` 计数，两者都不需要 Planner 自证。但 `dependency_change`、`auth_change`、`migration`、`ci_config` 如果完全交给 Planner 自己声明，就变成模型自己判断自己有没有风险，违反第 5 章"LLM 输出始终不可信"和"能力授权而非角色信任"的原则——一个漏判或被诱导隐瞒风险的 Planner 可以让高风险计划绕过 Plan Approval。因此 Policy Engine 额外维护一份与 `policy_profile` 绑定的路径规则表（如 `auth/**`、`**/migrations/**`、`.github/workflows/**`、依赖清单文件 `requirements*.txt`/`pyproject.toml` 等），对 `PlannedFileChange.path` 做独立正则匹配：命中规则表时无论 Planner 是否在 `risk_flags` 里声明，都强制视为对应风险类别，进入 Plan Approval。Planner 自报的 `risk_flags` 只能在规则表之外补充风险面（标记规则表未覆盖到的情形），不能通过不声明来降低已由路径规则判定的风险等级。

### 8.2 失败处理

| 故障 | 策略 |
|---|---|
| 模型 429/5xx | 同模型最多重试 3 次，指数退避加抖动；仍失败时仅允许切换到同一供应商、同一数据策略中预先批准的备用模型，并创建新 attempt |
| 输出结构无效 | 同模型修复一次，再失败则任务失败，不无限重试 |
| Worker 崩溃 | 由 Temporal Activity Timeout 和 Retry Policy 重新调度；Workflow 根据 Event History 恢复，不读取业务 Lease |
| 沙箱超时 | Activity 取消 Kubernetes Job/容器并终止完整进程组；销毁失败进入清理队列并告警 |
| Git 基线变化 | 不自动 rebase；创建新 Run 或请求人工确认 |
| 预算耗尽 | 停止新调用，保存现有 Artifact，返回 `budget_exceeded` |
| Activity 重复执行 | 依靠逻辑调用 ID、幂等键和唯一约束返回已有平台侧结果；外部模型调用按本节的未知完成策略处理 |

模型切换共享原 Run 的总预算，不重置 Token 或费用额度。并发调用必须先原子预留最大可能费用；预算耗尽后拒绝新预留，已在途调用按实际费用结算，因此最终费用允许存在一个配置化且可告警的在途调用上界。结构校验失败、内容策略拒绝和权限拒绝不得触发模型切换。v1 只处理公共仓库；后续私有仓库即使开放，也禁止未经租户书面配置跨供应商降级。所有模型切换必须记录原模型、目标模型、原因、attempt 和费用。

### 8.3 人工审批的超时与升级

`WAITING_REQUIREMENTS_APPROVAL`、`WAITING_PLAN_APPROVAL`、`WAITING_TEST_APPROVAL` 和 `WAITING_DELIVERY_APPROVAL` 不会无限期挂起：

- 默认超时：Requirements/Plan/Test Approval 24 小时，Delivery Approval 72 小时；租户可在 `policy_profile` 中覆盖。
- 达到 50% 超时时间点时，向该 Run 的审批人角色发送一次提醒；Workflow 等待审批 Update 期间不占用沙箱资源。
- 超时后默认转入 `FAILED`，`failure_reason="approval_timeout"`；不自动通过也不自动拒绝，避免高风险变更被静默放行。租户可显式配置"超时后升级给上级审批组重新计时一次"，而不是直接失败，但升级只允许一次，防止审批无限延后。
- 审批使用 Temporal Update，而不是无返回确认的单向 Signal。API 完成 RBAC 后提交带 `approval_id` 的 Update；Workflow Validator 校验当前状态和重复 `approval_id`，调用方只有在 Update 被接受并写入 History 后才收到成功响应。等待期间不持有沙箱或 worktree，恢复执行只使用已保存的 ArtifactRef。

### 8.4 Temporal 执行策略

- Workflow 代码保持确定性；数据库、模型、Git、对象存储和沙箱操作全部位于 Activity。
- 普通模型/Git Activity 设置 `Schedule-To-Start`、`Start-To-Close` 和指数退避 Retry Policy；参数校验、权限拒绝、预算拒绝属于不可重试错误。
- 超过 30 秒的沙箱 Activity 每 5 秒 Heartbeat 一次，并在 Heartbeat 处检查取消请求。取消后 10 秒内必须向容器或 Kubernetes Job 发出终止，随后确认完整进程树退出。
- Workflow History 达到内部阈值 5,000 个事件或序列化负载 25 MiB 时执行 Continue-As-New；只携带 ArtifactRef、预算余额和当前计划版本进入新 History。
- Workflow 发布使用 Temporal Worker Deployment Versioning；每次发布前用生产历史样本执行 replay test，禁止破坏确定性的代码直接接管仍在运行的旧 Workflow。
- Workflow ID 固定为 `tenant_id/run_id`，Workflow 启动采用 reject-duplicate 策略；API `Idempotency-Key` 映射到唯一 `run_id`，避免重复启动。

## 9. Agent 与工具协议

系统只定义四种会调用模型并产生非确定性判断的 Role：

| Role | 输入 | 结构化输出 | 禁止能力 |
|---|---|---|---|
| Planner | `TaskSpec`、`RepositorySnapshot` | `ChangePlan` | 写文件、执行命令、创建 PR |
| Developer | `WorkItem`、`DeveloperContext` | `PatchProposal` | 写入未授权路径、合并分支、创建 PR |
| QA | `TaskSpec`、基线快照；补充测试阶段可读取候选 diff | `TestPlan`（内部引用密封 Test Bundle Artifact） | 执行质量门禁、修改业务代码、批准交付 |
| Reviewer | `TaskSpec`、最终 diff、`VerificationReport` | `ReviewDecision` | 修改代码、跳过质量门禁、创建 PR |

Architect 不作为独立 Role，其职责合并进 Planner。Repairer 不作为独立 Role，修复由 Developer 使用 `WorkItem.kind="repair"` 执行。Repository Scanner、Dependency Scheduler、Policy Engine、Integration Worker、Model Gateway、Repository Service、Verification Service 和 Sandbox Service 全部是确定性组件，不命名为 Agent。`VerificationReport` 只能由 Verification Service 根据真实工具退出码和报告解析生成。

Agent 不是常驻对象，而是“策略 + 上下文构造器 + 可调用能力集合”。不同 Role 的输入输出通过 ArtifactRef 对齐，统一执行封装如下：

```python
class AgentExecutionRequest(BaseModel):
    role: Literal["planner", "developer", "qa", "reviewer"]
    work_item: WorkItem
    input_refs: list[ArtifactRef]
    policy_profile: str
    remaining_budget: Budget


class AgentExecutionResult(BaseModel):
    work_item_id: UUID
    attempt: int
    status: Literal["succeeded", "failed"]
    output_refs: list[ArtifactRef]
    model_call_ids: list[UUID]
    error_code: str | None = None


class Agent(Protocol):
    async def execute(
        self,
        request: AgentExecutionRequest,
        tools: ToolRegistry,
    ) -> AgentExecutionResult: ...
```

### 9.1 Tool Call 约束

- 工具参数必须通过 Pydantic/JSON Schema 验证。
- 所有路径在服务端规范化，拒绝绝对路径、`..` 和符号链接逃逸。
- 写文件工具同时校验租户策略、Run 权限和 WorkItem 的 `allowed_paths`。
- Shell 工具不接受单个字符串，只接受 `argv: list[str]`。
- 模型不能自行提高权限或修改 Tool Registry。
- 高风险工具调用需要 Policy Engine 批准，必要时等待人工确认。

### 9.2 上下文隔离

- Developer 只获得需求、目标文件、依赖接口摘要和必要代码片段。
- Reviewer 不获得 Developer 的推理对话，只读取补丁与验证结果。
- QA 在 Developer 执行前、独立上下文中生成密封验收测试。开发完成后 QA 可基于 diff 追加回归测试，但追加测试必须标记 `origin="post_patch"`，且不能替换或弱化原密封测试。QA 只设计测试；测试执行和通过判定属于 Verification Service。
- Secret Scanner 在内容送往外部模型前执行脱敏。

### 9.3 代码上下文检索与依赖摘要生成

"依赖接口摘要"和"必要代码片段"由 Repository Service 与确定性 Static Analyzer 生成，Developer 不直接遍历整个仓库：

- **基础索引**：对 base_revision 做一次静态分析，产出函数/类/公共接口的定义位置、签名和近似调用关系图。索引记录 `confidence` 和 `unresolved_references`；Python 动态导入、反射、装饰器注册等不能被 AST 可靠解析的部分不得标记为确定依赖。
- **增量接口 Artifact**：每个上游 Patch 经 Trusted Repository Service 应用后，Static Analyzer 针对变更文件生成 `ExportedInterfacePayload`，保存为 `ArtifactRef(kind="exported_interface")` 并绑定实际 `commit_sha`。它描述真实导出符号、签名变化、删除项和低置信度引用。
- **下游上下文版本**：只有依赖 WorkItem 的 patch 已集成后，下游才可调度。Repository Service 从该时刻的集成分支创建 `input_revision`，并用“基础索引 + 所有上游 exported-interface Artifact”构建 `DeveloperContext`。因此下游看到的是上游已经实现的接口，而不是旧版 base revision。
- **并行规则**：没有依赖边的 WorkItem 可以从同一集成基线并行执行；完成后由 Integration Worker 顺序应用。发生冲突时不自动选边，返回 Planner 或对应 Owner 修复。
- **按需检索**：若 Developer 判断摘要不足，可通过只读 Code Search 工具查询其依赖闭包。低置信度或存在 `unresolved_references` 时，策略允许扩大到仓库非敏感源码；服务端始终排除 `RepositorySnapshot.forbidden_paths`。如果完成任务必须读取或修改禁止路径，WorkItem 返回 `policy_denied`，Run 进入 `REJECTED` 且 `failure_reason="forbidden_path_required"`；系统不得向 Agent 返回被隐藏内容，也不得让模型自行猜测接口。
- **一致性校验**：Dependency Scheduler 生成 `WorkItem.dependencies` 时必须与 `ChangePlan.dependency_edges` 一一对应；两者不一致视为 Planning 阶段失败，退回 Planner。

## 10. Git 协作模型

Git 仓库和 worktree 只存在于可信的 Repository Service，不直接挂载进 Developer/QA 沙箱：

```text
bare-cache/{repo-id}.git
worktrees/{run-id}/{work-item-id}/
branches/run/{run-id}/{work-item-id}
```

执行与合并过程：

1. Repository Service 从 `input_revision` 导出不含 `.git`、Git 配置和凭证的源码归档。
2. Sandbox Service 把归档解包到 WorkItem 独占文件系统；Developer 只能通过路径受控工具修改文件，最终返回 unified diff Artifact。
3. Repository Service 在可信进程中规范化路径、解析 patch、拒绝越权路径/符号链接逃逸/二进制超限，再把 patch 应用到独立 worktree。
4. Static Analyzer 生成 `ExportedInterfacePayload` 并保存对应 Artifact；Repository Service 创建本地提交。沙箱不执行 `git commit`，也不接触 bare cache。
5. Integration Worker 按 DAG 顺序 cherry-pick 已校验提交。冲突时生成结构化冲突 Artifact，不使用 `-X theirs` 静默覆盖。
6. 合并后在全新沙箱重新运行全量质量门禁。
7. 只向远程推送最终候选分支，不推送中间 Agent 分支。

QA/Verification Service 或 Reviewer 发现问题时，根据 `VerificationReport.findings[].file_path`、`ReviewDecision.findings[].suggested_paths` 和 `ChangePlan.files[].owner` 将 Repair WorkItem 路由给原文件 Owner。Reviewer Finding 没有文件定位或横跨多个模块时返回 Planner 重新规划，不让调度器猜测 Owner。连续两次失败后不更换 Developer 继续盲修，而是回到 Planner 重新分析接口或拆分错误；重新规划会生成新 `ChangePlan` Artifact 并保留旧版本供审计。

GitHub App 短期安装令牌只注入 Trusted Repository Service，不进入 Agent 或沙箱。应用层拒绝推送非 `repopilot/{run_id}` 分支；接入仓库还必须配置 Branch Ruleset，保护默认分支且不得把 RepoPilot App 加入 bypass list。`contents:write` 是仓库级权限，不能单独作为“仅限候选分支”的安全保证。

## 11. 沙箱与安全设计

### 11.1 威胁模型

系统至少需要防御：

- 仓库文件中的 Prompt Injection。
- 恶意依赖安装脚本和测试脚本。
- 读取环境变量、云实例元数据或宿主机文件。
- Fork Bomb、内存耗尽、磁盘耗尽和超长进程。
- 通过网络外传源码或密钥。
- 路径穿越、符号链接逃逸和 Git Hook 执行。
- 日志或模型请求中的敏感信息泄露。

### 11.2 默认沙箱策略

- 非 root 用户、只读基础镜像、删除 Linux capabilities。
- 禁止 privileged、禁止挂载 Docker socket。
- 默认关闭出站网络。依赖解析在单独的 Build Sandbox 中通过只允许访问批准 Registry 的代理完成，产出带 hash 的依赖层；Developer/Verification Sandbox 只挂载该只读依赖层且保持断网。依赖安装脚本仍按不可信代码处理，Build Sandbox 不持有 Git、模型、数据库或对象存储凭证。
- 限制 CPU、内存、PID、磁盘、文件数量和执行时间。
- 使用 seccomp/AppArmor；要求 privileged、Docker socket、宿主设备、host network、内核模块、eBPF 或嵌套容器的任务视为超出 gVisor 风险边界，在 v1 直接拒绝。
- 清除继承环境变量；Agent/QA/验证沙箱不注入模型、Git、数据库或对象存储凭证。模型调用、Git 操作和 Artifact 上传分别由可信 Model Gateway、Repository Service 和 Artifact Service 代理。依赖代理如需认证，只使用绑定沙箱身份、只读、短生命周期且不能访问其他服务的工作负载令牌。
- 禁用仓库内 Git hooks，依赖安装阶段和测试阶段使用不同权限配置。
- 超时后终止完整进程树，而不只是父进程。
- 送入模型前，对仓库文件、Issue 正文、依赖包 README 等外部文本执行 Prompt Injection 启发式检测（已知注入模式的规则/分类器，如"忽略前述指令""以系统身份运行"等）。命中时不直接阻断任务，而是将该 Run 标记为高风险并强制进入人工 Plan Approval，同时在审计记录中标注可疑来源，供规则迭代使用。这一层是纵深防御的第一道检测，最终仍以 9.1 的能力最小化和输出结构校验作为兜底，不单独依赖检测结果放行或拒绝。

### 11.3 供应链安全

- 依赖必须来自允许的 Registry。
- 生产模式优先使用 lockfile 和 hash 校验。
- 输出 SBOM，并对容器与依赖执行漏洞扫描。
- 模型、Prompt、策略配置和基础镜像全部记录不可变版本。

## 12. API Demo

### 12.1 创建任务

外部请求模型与内部 `TaskSpec` 分离。客户端只能提交 `CreateRunRequest`，不能设置 `tenant_id`、`acceptance_criteria_source`、最终 `base_revision` 或 Policy 判定；这些字段由认证上下文和 Ingest 阶段生成。

```python
class CreateRunRequest(BaseModel):
    repository: RepositoryRef
    requirement: str
    acceptance_criteria: list[str] | None = None
    budget: Budget
```

```http
POST /v1/runs
Idempotency-Key: 6ab1...

{
  "repository": {
    "provider": "github",
    "owner": "example",
    "name": "shop-service",
    "base_revision": "8f2a..."
  },
  "requirement": "修复并发结账时库存可能变成负数的问题",
  "acceptance_criteria": [
    "库存不得小于 0",
    "保留现有 API 兼容性",
    "新增并发测试"
  ],
  "budget": {
    "max_cost_usd": 5,
    "max_wall_time_seconds": 1800,
    "max_model_calls": 40,
    "max_sandbox_seconds": 1500
  }
}
```

响应：

```json
{
  "run_id": "run_01J...",
  "status": "queued",
  "status_url": "/v1/runs/run_01J..."
}
```

认证使用 OIDC 登录后的租户访问令牌；服务端从令牌得到 `tenant_id` 和 RBAC，不接受请求体覆盖。`Idempotency-Key` 在同一租户内保留 24 小时：相同 key、相同规范化请求体返回原 `run_id`；相同 key、不同请求体返回 `409 idempotency_conflict`。请求体上限 256 KiB。统一错误响应包含 `error_code`、`message`、`trace_id` 和可选 `retry_after_seconds`；容量拒绝返回 `429` 与 `Retry-After`。

### 12.2 查询与取消

```http
GET  /v1/runs/{run_id}
GET  /v1/runs/{run_id}/events?cursor=...
GET  /v1/runs/{run_id}/artifacts
POST /v1/runs/{run_id}:cancel
```

API 使用租户级 RBAC。`/events` 返回脱敏后的领域进度和 `audit_events` 投影，不暴露原始 Temporal Event History；cursor 是不透明、稳定排序的分页令牌。Artifact 下载使用短时签名 URL，并记录审计事件。取消接口幂等：终态 Run 返回当前终态；非终态 Run 向 Temporal 发出 Workflow Cancellation Request，在服务端确认请求已接受后返回 `202`。审批使用 Update，取消不复用审批 Update。

### 12.3 GitHub 接入（Webhook）

GitHub App 安装到目标仓库后，Issue 打上约定标签（如 `repopilot:fix`）会触发 Webhook：

```http
POST /v1/webhooks/github
X-Hub-Signature-256: sha256=...
```

网关先用原始请求体校验 HMAC 签名，再校验 `event=issues`、允许的 action 和 GitHub App installation 与租户/仓库绑定。`X-GitHub-Delivery` 作为唯一键保存 7 天，重复投递直接返回已有接收结果。有效事件转换为 `CreateRunRequest`：`requirement` 取自 Issue 标题与正文；`acceptance_criteria` 由 Ingest 阶段按 8.1 step 1 的优先级抽取。Issue 未结构化列出验收条件时标记为 `acceptance_criteria_source="heuristic"` 并强制进入 Plan Approval。Webhook 端点在持久化接收结果并成功启动或确认已有 Temporal Workflow 后立即返回 `202`，不等待 Agent 执行。

GitHub App 权限范围最小化为目标仓库的 `contents:write`、`pull_requests:write`、`issues:read`；不申请组织级、`administration` 或 `workflows` 权限。`contents:write` 本身是仓库级权限，候选分支限制由 Repository Service 的分支名校验和仓库 Branch Ruleset 共同保证；RepoPilot App 不进入默认分支 ruleset 的 bypass list。安装范围由 22 章列出的产品开放问题决定。

## 13. 存储设计

建议的 PostgreSQL 核心表：

| 表 | 用途 |
|---|---|
| `tasks` | 稳定的用户任务定义 |
| `run_projections` | Temporal Workflow 的只读查询投影；不推进状态 |
| `work_item_projections` | DAG 节点、依赖、attempt 和展示状态；不拥有调度 Lease |
| `audit_events` | 登录、审批、策略、Artifact 下载和 GitHub 副作用等只追加审计事件；不是 Workflow History |
| `artifacts` | 产物元数据和内容摘要 |
| `model_calls` | 模型、Token、费用、延迟和错误 |
| `policy_decisions` | 权限判断及依据 |
| `usage_ledger` | 租户预算预留与结算账本 |
| `webhook_deliveries` | GitHub Delivery 去重和接收结果 |

所有租户相关表必须包含 `tenant_id`。访问层默认追加租户过滤条件，关键环境可使用 PostgreSQL Row-Level Security 作为第二道防线。

日志和 Artifact 设置生命周期：原始模型对话、源码派生索引和普通 Artifact 默认保留 30 天，审计摘要保留 180 天；企业租户只能在合规策略允许范围内调整。完整仓库快照只使用短生命周期对象，Run 终止后 24 小时内删除。删除任务覆盖业务数据库、对象存储和代码索引；Temporal History 按 Namespace Retention Policy 到期清理，API 必须向用户明确它不受即时业务删除控制。

## 14. 可观测性

### 14.1 Trace

每个请求携带：

- `trace_id`
- `tenant_id`
- `run_id`
- `work_item_id`
- `attempt`
- `model_call_id`
- `sandbox_id`

使用 OpenTelemetry 串联 API、Temporal Workflow/Activity、模型网关、Repository Service 和沙箱。

### 14.2 Metrics

- Run 成功率、取消率、失败原因分布。
- 各阶段 P50/P95/P99 延迟。
- 模型调用成功率、Token、费用和限流次数。
- Temporal Task Queue backlog、Schedule-To-Start 延迟、Activity Timeout、Heartbeat Timeout 和重试次数。
- 沙箱启动延迟、OOM、超时和网络拒绝次数。
- 补丁大小、测试通过率、Reviewer 驳回率。

### 14.3 日志与审计

应用日志采用结构化 JSON。默认不记录 Secret、完整源码和未经脱敏的 Prompt。审计日志独立存储并设置追加写权限，至少覆盖登录、仓库授权、策略变更、Artifact 下载和 PR 创建。

## 15. 容量规划与背压

- API 只负责接收任务，不同步等待 Agent 执行完成。
- v1 使用按能力拆分的 Temporal Task Queue（`model`、`repository`、`sandbox`、`delivery`），不按每个租户创建高基数队列。
- Worker 分别设置最大并发；Model Gateway、Sandbox Service 再执行全局、租户、仓库和供应商四级信号量限制。
- Phase 0/1 单租户；Phase 2 的 v1 Production 引入独立 Capacity Manager。Workflow 通过 Activity 申请带 TTL 的容量 Permit，未获得 Permit 时使用 Temporal Timer 退避，不创建沙箱或调用模型。Permit 只控制容量，不保存或推进 Run 状态。
- 当模型或沙箱容量不足时，Worker 降低 Activity 并发或 Workflow 等待容量 Permit，避免无限创建进程。
- Run 创建接口根据队列水位返回预计开始时间；达到硬上限时返回 `429`。

初期不预设大规模容量数字。上线前使用压测得到单 Worker 吞吐、沙箱冷启动和数据库事件写入上限，再计算副本数。

## 16. 测试与评测策略

### 16.1 工程测试金字塔

- 单元测试：状态转换、DAG、预算、路径权限、命令解析、幂等处理。
- 契约测试：模型供应商、Git Provider、沙箱和对象存储适配器。
- 集成测试：Temporal Test Server + PostgreSQL + MinIO + Fake Model + Fake Git Server。
- 故障注入：Worker 崩溃、重复消息、数据库瞬断、模型 429、沙箱 OOM。
- 安全测试：路径逃逸、Prompt Injection、命令注入、Secret 外传。
- E2E：从固定仓库提交任务，最终补丁必须通过独立验证容器的测试。

### 16.2 Agent 评测

评测集至少包含：

- 小型算法或单文件 Bug。
- 跨文件 API 修改。
- 并发、事务和状态一致性问题。
- 需求不完整或相互矛盾的任务。
- 含诱导指令和恶意测试脚本的安全任务。

实验必须固定仓库 revision、模型版本、Prompt 版本、温度和预算。至少对比：

1. 单 Agent 基线。
2. Planner + Developer。
3. Planner + Developer + Reviewer。
4. 完整多 Agent + 修复闭环。

报告 resolve rate、回归率、费用、耗时和方差，避免只展示成功案例。

## 17. 部署与发布

### 17.1 环境

- `dev`：Docker Compose 启动 FastAPI、Temporal 开发服务、PostgreSQL、MinIO 和 rootless Docker 沙箱；允许 Fake Model。
- `staging`：Kubernetes 部署 API/Worker，使用 Temporal 测试 Namespace 和 `RuntimeClass=runsc` 的 gVisor 沙箱，只操作测试组织的公共仓库。
- `production`：Kubernetes 部署 API/Worker，使用托管 Temporal 服务、托管 PostgreSQL、对象存储和 gVisor；只允许签名镜像、审批过的策略配置和公共 GitHub 仓库。

### 17.2 发布策略

- 数据库迁移采用 expand/migrate/contract，不与破坏性代码变更同时上线。
- Worker 支持版本化任务；旧消息必须由兼容 Worker 消费或迁移。
- 新模型或 Prompt 先跑离线回归，再进行租户级 canary。
- 当成本、失败率或安全拒绝率异常时自动回滚配置。
- Kill Switch 可以停止模型调用、停止沙箱创建或禁用 PR 写入。

### 17.3 灾备与备份

- **PostgreSQL**：托管服务的每日自动全量备份 + WAL 连续归档，RPO ≤ 5 分钟、RTO ≤ 1 小时；定期演练恢复流程。
- **Temporal**：依赖托管 Temporal 服务的跨可用区复制；Event History 是 Run 恢复的唯一真实来源（见 6.4），任何 Run 的重放都以其为准，不依赖 PostgreSQL 投影的完整性——投影丢失只影响查询展示，不影响可恢复性。
- **对象存储**：开启版本控制与生命周期策略，防止 Artifact 被误删或覆盖；跨区域复制作为企业租户可选项，不是 v1 默认项。
- **单可用区故障**：API/Worker 按多可用区部署，由 Kubernetes 自动重调度；沙箱是短生命周期资源，故障时直接放弃重建而非做沙箱级容灾。
- **单区域灾难恢复**：v1 只承诺单区域内的高可用，不承诺跨区域灾备；这是有意的范围裁剪（详见 3.2 非目标），跨区域方案留待 Phase 2 之后根据实际容量需求评估，避免在负载画像未知前过度设计。

## 18. 配置与密钥管理

- 非敏感配置通过版本化配置中心管理。
- API Key、Git Token 存放于 Secret Manager，不写入数据库和日志。
- Secret 采用最小权限和短生命周期，支持自动轮换。
- 每次 Run 记录配置快照的版本号，而不是复制明文 Secret。
- Prompt 作为版本化制品发布，需要 Code Review 和离线评测门禁。

## 19. 主要风险与取舍

| 决策 | 收益 | 代价 |
|---|---|---|
| 多 Agent 独立上下文 | 降低上下文污染，角色职责清晰 | 模型调用和延迟增加 |
| Git worktree 隔离 | 支持真实并行，减少分支互相干扰 | 磁盘使用和清理复杂度增加 |
| 默认关闭沙箱网络 | 显著降低外传和供应链风险 | 动态安装依赖更复杂 |
| 事件驱动状态机 | 可恢复、可审计、可扩展 | 需要处理幂等和最终一致性 |
| Reviewer 独立调用 | 降低确认偏差 | 可能重复读取上下文并增加费用 |

多 Agent 不是默认答案。对于单文件、小改动任务，路由器可以选择单 Agent 快速路径，以降低成本和延迟。

## 20. 分阶段交付计划

### Phase 0：可验证原型（本地开发，不宣称生产可用）

- FastAPI + Temporal 开发服务 + PostgreSQL + MinIO，单仓库、单租户、Fake Model。
- Temporal Workflow 持久化 Run 编排状态，PostgreSQL 保存查询投影和基础 Artifact 元数据。
- rootless Docker 沙箱、结构化命令、硬预算。
- 单 Agent 基线与 20 个固定评测任务。

### Phase 1：Staging Candidate（不对外承诺生产 SLO）

- Planner / Developer / QA / Reviewer 四 Role 分工。
- DAG 调度、worktree 隔离、失败恢复。
- GitHub App、候选 PR、RBAC、审计日志。
- Kubernetes staging、gVisor、监控告警和安全测试。

### Phase 2：v1 Production

- 托管 Temporal/PostgreSQL、生产 Kubernetes、多租户公平调度和配额。
- 多模型路由、缓存和降级。
- v1 对超出 gVisor 风险边界的任务直接拒绝；microVM 作为独立后续项目评估，不进入当前承诺范围。
- 不少于 100 个分层任务的发布评测、在线反馈回流、自动回归评测和 canary。

## 21. v1 验收标准

Phase 2 完成并进入 v1 上线前必须同时满足：

- Worker 在任意状态被强制终止后，任务能够恢复且不重复产生 Git 副作用。
- Agent 无法写入 `allowed_paths` 之外的文件。
- 恶意仓库无法读取控制平面 Secret 或访问云元数据服务。
- 所有 Shell 执行均通过结构化参数进入沙箱，不在宿主机执行。
- 预算耗尽后不再批准新的费用预留；已在途调用的最大超额不超过配置的并发预留上界，并触发告警。
- 每次交付可以追溯到固定的代码 revision、模型、Prompt、策略和基础镜像。
- 候选补丁必须在全新验证沙箱中通过质量门禁。
- 单 Agent 与多 Agent 的离线对比报告可以复现。

## 22. 已确定的关键决策与剩余产品问题

已确定：

1. Temporal 是唯一顶层工作流引擎，LangGraph 不进入当前主架构。
2. 本地原型不使用 Kubernetes；staging 和 production 使用 Kubernetes。
3. 本地使用 rootless Docker，staging/production 使用 gVisor；v1 不支持 microVM。
4. 首个产品场景固定为 Python 公共 GitHub 仓库的 Issue 修复，SWE-bench 只用于离线评测。
5. v1 不接收私有仓库，避免在数据保留和模型供应商策略未完成前处理私有源码。
6. 高风险计划需要 Plan Approval；所有候选 PR 都需要 Delivery Approval。
7. 原始模型对话和脱敏执行日志保留 30 天，审计摘要保留 180 天；完整仓库快照不进入 Artifact Store，沙箱销毁时删除。租户删除请求必须在 24 小时内清除业务数据库和对象存储数据，法定审计记录除外。
8. Phase 0 是本地原型，Phase 1 是 Staging Candidate，Phase 2 完成后才称为 v1 Production 并承诺第 4 章 SLO。

仍需产品负责人确定、但不影响技术骨架的问题：

1. 首批允许接入的 GitHub 组织和仓库列表。
2. 人工审批人的 RBAC 角色及升级路径。
3. 单租户默认费用额度和最大并发。
4. 30/180 天保留期是否满足目标客户所在地区的合规要求。

## 23. 建议的后续文档

这份 Demo 确认方向后，应拆成以下可实施文档：

- `ADR-001-workflow-engine.md`：记录选择 Temporal、排除自研状态机和 LangGraph 顶层编排的依据。
- `ADR-002-sandbox-isolation.md`：记录 rootless Docker/gVisor 的隔离边界、运行参数和 v1 拒绝策略。
- `ADR-003-repository-context-index.md`：记录 9.3 所述代码索引的具体实现选型（静态分析工具链、索引存储、跨语言扩展路径）。
- `api/openapi.yaml`：正式 API 契约，涵盖 12.1-12.3。
- `schemas/events.md`：API 进度事件、审计事件与 Temporal History 投影的边界和兼容规则。
- `security/threat-model.md`：完整威胁模型与安全控制。
- `runbooks/worker-backlog.md`：Temporal Task Queue 积压处置手册。
- `runbooks/model-provider-outage.md`：模型供应商故障手册。
- `evaluation/benchmark-design.md`：数据集、基线和统计方法。
- `operations/slo-alerts.md`：SLO、告警阈值和错误预算。

---

本 Demo 的核心判断是：生产级多智能体系统的难点并不在于创建多少个 Agent，而在于把不确定的模型行为放进一个可恢复、可验证、可限权、可审计的确定性系统中。

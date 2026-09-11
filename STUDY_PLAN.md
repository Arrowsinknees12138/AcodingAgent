# mini-swe-agent 学习与项目实施计划

> 源码位置：`reference/mini-swe-agent/`（已克隆到本地，只读研究，不在这个目录里改代码）
> 目标：2-4天内把 mini-swe-agent 的核心逻辑吃透到"面试问不倒"的程度，然后在它之上实现你自己的 Reviewer 模块和评测框架。
> 用法：按顺序做，每个任务后面的问题自己先想/写下来再核对源码，做完打勾。

---

## Day 0：环境准备（半天内搞定）

- [ ] 进入 `reference/mini-swe-agent`，用 `pip install -e .`（或 `uv pip install -e .`）装成可编辑模式
- [ ] 准备至少一个模型 API Key（Anthropic/OpenAI 均可，litellm 统一适配），设置到环境变量
- [ ] 跑通一次最小示例：`python -m minisweagent.run.hello_world -t "写一个能通过测试的函数" -m <你的模型名>`
- [ ] 确认能看到完整的日志输出（`logging.basicConfig(level=logging.DEBUG)` 已经在 hello_world.py 里开了）

---

## Day 1：精读核心源码（这是重头戏，别跳）

### 任务1：`src/minisweagent/agents/default.py` —— 整个项目的心脏

先通读一遍，再逐个回答（答案我已经核对过源码，卡住了直接看代码对答案）：

- [ ] `AgentConfig` 里的 `step_limit` / `cost_limit` / `wall_time_limit_seconds` / `max_consecutive_format_errors`，分别是在哪个方法里被检查、超限后抛什么异常？（提示：都在 `query()` 里检查，分别抛 `LimitsExceeded` 和 `TimeExceeded`）
- [ ] `run()` 里的 `while True` 循环靠什么条件退出？（提示：判断 `self.messages[-1].get("role") == "exit"`，不是判断某个 bool 标志位）
- [ ] 为什么每次 `step()` 后都在 `finally` 里调用 `self.save()`，而不是等整个任务跑完才存一次？这跟"任务中途 crash 也不能丢失执行记录"这个可靠性要求有什么关系？
- [ ] `step()` / `query()` / `execute_actions()` 三者的职责边界分别是什么？谁负责调模型、谁负责判断是否超限、谁负责真正执行动作？
- [ ] 项目用**异常**（`FormatError` / `InterruptAgentFlow` / `LimitsExceeded` / `TimeExceeded`）来控制主循环的跳出，而不是用返回值判断。这是"用异常做控制流"的设计——你怎么评价这个取舍？相比 if/return 的方式，优点和代价分别是什么？（这是很容易被问到、也很适合展开讲的一个设计点）
- [ ] `serialize()` 里为什么要把 `self.model.serialize()` 和 `self.env.serialize()` 也合并进去，而不只存 messages？这跟"可复现性（记录 model 配置、环境配置）"这个目标有什么关系？

### 任务2：`src/minisweagent/exceptions.py`（很短，但结构很关键）

- [ ] 自己画一下继承关系：`InterruptAgentFlow` 是基类，`Submitted` / `LimitsExceeded` / `UserInterruption` / `FormatError` 都继承它，`TimeExceeded` 又继承 `LimitsExceeded`
- [ ] 想一想：为什么 `TimeExceeded` 要继承 `LimitsExceeded` 而不是直接继承 `InterruptAgentFlow`？（提示：跟 `default.py` 里 `except` 块的捕获顺序/复用处理逻辑有关）

### 任务3：`src/minisweagent/environments/local.py`

- [ ] `execute()` 怎么执行一条 shell 命令？为什么用 `subprocess.Popen` + 手动 `os.killpg` 管理进程组，而不是直接 `subprocess.run(timeout=...)`？（提示：防止超时后子进程变成孤儿进程继续占用资源，这是"命令超时清理"最容易被忽略的细节）
- [ ] `_check_finished()` 这个方法是全文件最值得琢磨的地方：它怎么判断"任务已经提交完成"？（答案：检测命令输出的**第一行**是不是魔法字符串 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 且 `returncode == 0`，是就把后续内容当作 `submission`，抛出 `Submitted` 异常）
- [ ] 想清楚这意味着什么：这个 Agent 的"提交答案"机制，**不是**一个专门的 `end_task` 工具/API 原生 tool call，而是让模型执行一条普通 shell 命令、输出一个特定字符串来触发退出。对比"结构化输出"这个更严谨的思路，这里为什么反而用了一个字符串匹配的"土办法"？这是不是一种简单性换取的权衡？——**这是全项目最值得在面试里主动讲的一个设计点，因为它体现了"minimal但能打74%"背后真正的取舍逻辑，而不是随便简化。**

### 任务4：对比 `models/litellm_model.py`、`models/utils/actions_text.py`、`models/utils/actions_toolcall.py`

- [ ] 模型的回复是怎么被解析成"action（要执行的 shell 命令）"的？这个项目支持两种方式：一种是从模型的自然语言回复里用正则提取 bash 代码块（text-based），一种是用 LLM API 原生的 tool-calling 能力（toolcall-based）
- [ ] 对照 `config/mini.yaml` 和 `config/mini_textbased.yaml` 这两份配置，分别对应哪种解析方式？
- [ ] 想一想：`default.py` 里的 `FormatError` 异常，主要是在哪种解析方式下更容易触发？（提示：text-based 靠正则提取，模型输出格式一旦跑偏就会解析失败；toolcall-based 靠 API 结构化保证格式，出错概率更低——这也是很多真实系统从 text-based 迁移到 toolcall-based 的原因）

### 任务5：`src/minisweagent/run/hello_world.py` + `src/minisweagent/config/default.yaml`

- [ ] 读完 `hello_world.py` 应该能自己不看着重新写一遍：`DefaultAgent(LitellmModel(...), LocalEnvironment(), **config)` 这几行怎么组装起来的
- [ ] 打开 `config/default.yaml`，读一遍 `system_template` 和 `instance_template` 的实际 prompt 内容——**代码只是骨架，prompt 才是真正决定 Agent 行为的地方**
- [ ] 找到 prompt 里是怎么教模型使用 `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` 这个魔法字符串提交任务的

### Day 1 产出物（自查，做不到说明还没吃透）

- [ ] 不看代码，能在纸上/白板画出一次完整 `agent.run()` 的时序图：System Message → User Message → 循环{query → execute_actions} → 命中 `Submitted`/`LimitsExceeded`/... → exit
- [ ] 能解释清楚 `agent.serialize()` 输出结构里每个字段（`info.model_stats`、`info.exit_status`、`info.submission`、`messages`、`trajectory_format`）分别是什么
- [ ] 实际跑通至少一次 `hello_world.py`，在一个真实任务上看到完整的 message 序列输出
- [ ] 写一份不超过1页的"这个项目的5个关键设计决策"笔记，建议包括：①用异常做控制流 ②魔法字符串做任务提交而非结构化 tool call ③text-based vs toolcall-based 两种 action 解析方式并存 ④每步执行后立即落盘保存 trajectory ⑤成本/步数/时间三种限制统一在 `query()` 里检查

---

## Day 2-3：实现你自己的 Reviewer 模块（这部分是你自己的原创代码）

不在 `reference/` 目录里改，新建自己的项目代码目录（比如 `src/repopilot/`）。

- [ ] 设计 Reviewer 的输入：`task`（原始任务描述）+ `diff`（对应 mini-swe-agent 跑完后 `agent.run()` 返回的 `submission`）+ `test_results`（你自己封装的测试执行结果）
- [ ] 用 pydantic 定义结构化 Finding Schema（可以直接借鉴 `default.py` 里大量用 `pydantic.BaseModel` 定义 Config 的风格，保持代码风格一致）
- [ ] Reviewer 内部**新起一次独立的 LLM 调用**，不能复用 `agent.messages`（这是"独立 Context 减少 confirmation bias"的关键，别图省事直接把 agent 的历史传进去）
- [ ] 定义 PASS / CHANGES_REQUESTED 判定规则（比如：出现 BLOCKER/MAJOR 级别的 Finding 就是 CHANGES_REQUESTED）
- [ ] 如果 CHANGES_REQUESTED，怎么发起第二轮？参考 `run()` 方法里 `task` 是怎么通过 `instance_template` 渲染进第一条 user message的（`self.extra_template_vars |= {"task": task, ...}`），你的二轮 followup task 可以把 Review 反馈拼接进新的 task 字符串，重新调一次 `agent.run(followup_task)`，轮数限制在 1-2 次

---

## Day 4：编排层 + Trajectory/Report 封装

- [ ] 写一层薄的编排代码：`Task → Sandbox/Environment → agent.run() → 你的 Reviewer → FinalReport`
- [ ] FinalReport 里包含：files changed、test 结果、review 结论、cost（直接从 `agent.serialize()["info"]["model_stats"]` 里取）、duration
- [ ] 这一层不需要复杂框架，几十行胶水代码即可，重点是让整个链路可以一条命令跑完并产出一份结构化 JSON

---

## Day 5：真实评测对比（10-15个 SWE-bench Verified 任务）

- [ ] 参考（不是照抄）官方的 `src/minisweagent/run/benchmarks/swebench.py`，理解它是怎么批量跑任务、怎么收集结果的
- [ ] 挑 10-15 个 SWE-bench Verified 任务，分别跑：①纯 baseline（只用 mini-swe-agent）②baseline + 你的 Reviewer
- [ ] 记录两组的 resolve rate、平均 cost、平均耗时，做成一张对比表——这就是你的 Ablation Study 数据

---

## Day 6-7：打磨交付物

- [ ] README：清楚写明"执行引擎依赖官方 mini-swe-agent（MIT 协议），我自己实现的是 Reviewer 阶段 + 评测框架"
- [ ] 架构图（一张图说明 Task → Environment → Agent → Reviewer → Report 的流转）
- [ ] 一段关于 Prompt Injection 防护 / Secret 管理的设计说明（哪怕实现简单，想清楚了就值得写，AI Agent 方向面试大概率会问）
- [ ] 3-5 分钟 demo 录屏：跑一个真实 issue，展示 baseline 和 +Reviewer 的差异
- [ ] "Known Limitations / 如果有更多时间会做什么"——主动提未完成的 Planner、更大规模评测等，展示你清楚项目的边界

---

## 附录：面试高频追问自测（读完源码后自己对着答一遍）

1. Agent 怎么知道自己该停下来了？有哪几种退出路径？
2. 如果模型输出格式不对（比如没有按预期给出 bash 代码块），系统怎么处理？会无限重试吗？
3. 为什么 trajectory 要在每一步之后就存盘，而不是最后统一存？
4. 你的 Reviewer 为什么要用独立的 LLM 调用而不是把 review 这一步也塞进 Agent 自己的循环里？
5. mini-swe-agent 本身的成本/步数/时间限制机制，跟你自己编排层要做的 Cost Control 是什么关系——是重复造轮子还是分层复用？
6. 如果要让这个系统真正在 Docker Sandbox 里跑（而不是 `LocalEnvironment` 直接在宿主机执行），你需要改动/替换哪一层？（提示：`environments/docker.py` 已经有官方实现，看一眼它跟 `local.py` 的接口是不是完全一致）

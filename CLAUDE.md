# AI Agent Framework - CLAUDE.md

* **单一职责**：一个模块、类、函数只负责一类清晰职责。
* **禁止重复造轮子**：优先复用成熟开源方案，非核心能力不自研。
* **导入前置**：`import` 原则上统一放在文件头部。
* **最小暴露**：非公开能力默认私有，减少无必要的对外接口。
* **命名清晰**：名称必须表达职责，避免缩写和含糊命名。
* **显式优于隐式**：禁止依赖隐藏副作用和隐式状态流转。
* **类型优先**：公开接口必须补全类型标注。
* **数据与行为分离**：数据模型不承载复杂业务逻辑。
* **面向接口编程**：依赖抽象，不直接耦合具体实现。
* **默认不可变**：能不用可变状态就不用可变状态。
* **异常要分类**：不要抛裸异常，错误类型要明确。
* **失败可解释**：错误信息必须可读、可定位、可处理。
* **函数尽量短小**：单个函数尽量只完成一个完整动作。
* **避免深层嵌套**：优先早返回，减少多层 `if/else`。
* **禁止魔法值**：重复使用的常量必须提取命名。
* **配置外置**：可变参数放配置，不写死在逻辑中。
* **副作用集中**：I/O、网络、数据库调用集中在边界层。
* **注释解释原因**：注释优先说明"为什么"，不是重复"做什么"。
* **兼容性优先**：公共接口变更必须考虑向后兼容。
* **测试友好**：设计必须便于 mock、替换和单元测试。
* **边界清晰**：跨层调用必须通过正式接口，禁止越层访问。
* **一处定义**：同一规则、常量、协议只保留一个权威定义。
* **代码审核**：由 codex 排查代码是否和需求一致

## Project Overview
Offline-first, extensible AI Agent Framework in Python 3.11+ / pydantic v2.
Protocol → Base → Default three-layer pattern. Tech: structlog, blinker, litellm, SQLite, MCP SDK, A2A SDK.

## Architecture Layers
```
agent_framework/
├── agent/           # Agent loop, coordinator, state, prompt templates, skills
├── adapters/model/  # 20 LLM adapters (OpenAI/Anthropic/Google/DeepSeek/Groq/OpenRouter/Together/...)
├── context/         # Context engineering, compression, 5-slot builder
├── graph/           # LangGraph-compatible compiled graph engine
├── hooks/           # Hook registry, executor, 3 builtin hooks, payloads
├── infra/           # Config, logging, event bus, telemetry
├── memory/          # Memory manager, SQLite/PostgreSQL/MongoDB/Neo4j stores
├── models/          # Pydantic v2 data models (message/tool/agent/subagent/hook/plugin)
├── notification/    # [planned] RuntimeNotificationChannel, BackgroundNotifier
├── plugins/         # Plugin manifest, loader, lifecycle, permissions
├── protocols/       # core.py protocols + MCP client + A2A client adapter
├── skills/          # Skill loader, preprocessor, SKILL.md discovery
├── subagent/        # Factory, scheduler, runtime, delegation, HITL, interaction channel
├── tools/           # Tool executor, catalog, registry, decorator, builtin tools, shell
├── entry.py         # AgentFramework facade
├── cli.py           # CLI entry point
├── main.py          # Interactive terminal
├── terminal_runtime.py  # Rich terminal display
└── textual_cli.py       # Textual TUI
config/              # Model & collection strategy JSON configs
tests/               # 1358+ tests across 35+ files
```

## Completed Tasks
- **L1-8** 全模块骨架 (infra/models/protocols/adapters/tools/memory/context/agent)
- **#11-16** ReAct Agent, SubAgent Runtime, MCP/A2A Client, Entry+CLI, Integration
- **#17-27** 多智能体协调 + v2.4架构 (Policy/Config/SessionState/AgentLoop/Skill/MemoryScope)
- **#29-37** 多模型适配(20+) + Skill系统 + 交互终端main.py
- **#38-39** 终止条件6层闭环 + 全链路日志50+事件
- **#40-52** 架构审查+收口: RunCoordinator三层拆分, Hook/Decision分离, TerminationKind, CommitSequencer, SubAgent状态机, SessionSnapshot, 架构守卫43项
- **#53** 记忆+上下文闭环修复
- **s03-s08** 任务系统 + 后台执行: 持久化任务 DAG, 依赖图自动解锁, 独立 subprocess 后台并发, 跨 run 通知持续, SIGKILL 进程组取消链
- **Graph** LangGraph 兼容编译式图引擎: StateGraph/CompiledGraph/InMemorySaver, 条件路由, fan-out/fan-in
- **TUI** 流式渲染修复: #active-stream 实时 partial line
- **v3.1 长时交互**: 15 态统一状态机 + PauseReason + DelegationEvent 事件通道 + HITL 闭环 + Checkpoint + RuntimeNotificationChannel + AckLevel 四级确认
- **v3.1 收集策略**: 三种结果收集模式 (SEQUENTIAL/BATCH_ALL/HYBRID) + LeadCollector + config JSON 控制 + progressive 共存
- **v3.1 适配器扩展**: 新增 10 个 API 厂商 (OpenRouter/Together/Groq/Fireworks/Mistral/Perplexity/SiliconFlow/Moonshot/Baichuan/Yi) + 多模态能力声明

## Key Design Patterns

### 核心架构
- **RunCoordinator三层**: Coordinator(编排) + StateController(状态) + PolicyResolver(配置)
- **AgentLoop零写入**: 返回 IterationResult, 不直接修改 AgentState/SessionState
- **MessageProjector**: 格式化与状态分离, 返回 message 列表供 RunStateController 提交
- **SessionSnapshot只读**: 上下文层消费冻结快照, 不直接读可变 SessionState

### 策略与配置
- **策略解释权唯一**: ContextPolicy→ContextEngineer, MemoryPolicy→MemoryManager, CapabilityPolicy→授权链
- **Config→Policy链路**: FrameworkConfig → _bind_config_policies() → agent.get_*_policy() → apply_*_policy()
- **配额硬软语义**: 硬(超出→拒绝) vs 软(超出→降级), 每个配额有唯一 Owner

### 工具与权限
- **工具命名**: `local::<name>`, `mcp::<srv>::<name>`, `a2a::<alias>::<name>`, `subagent::spawn_agent`
- **权限链**: schema导出(可见性) → is_tool_allowed()(安全) → on_tool_call_requested()(agent hook)
- **batch_execute顺序**: asyncio.gather 保证结果按输入顺序返回

### 子Agent + 长时交互 (v3.1)
- **SubAgentStatus 15态**: PENDING→QUEUED→SCHEDULED→RUNNING→PAUSED/TERMINAL, PauseReason 正交
- **DelegationEvent通道**: append-only, sequence_no 单调递增, AckLevel 四级 (NONE→RECEIVED→PROJECTED→HANDLED)
- **HITL闭环**: sub-agent QUESTION event → DelegationExecutor → HITLHandler → user → resume_subagent
- **三种收集策略**: SEQUENTIAL(逐个汇报) / BATCH_ALL(全部等待) / HYBRID(批次拉取, 默认)
- **Config 控制**: `subagent.default_collection_strategy` + `collection_poll_interval_ms`, LLM 可 per-spawn 覆盖
- **progressive 共存**: execution_mode=progressive 控制迭代内流式, collection_strategy 控制迭代间批次, 不冲突

### 记忆管理
- **会话成对**: begin_run_session / end_run_session (finally), record_turn → CommitDecision
- **SubAgent 记忆**: Isolated/InheritRead/SharedWrite, spawn 时冻结快照

### 终止与错误
- **终止6层**: LLM_STOP → MAX_ITERATIONS → OUTPUT_TRUNCATED → ERROR(3次) → USER_CANCEL → timeout
- **TerminationKind**: NORMAL / ABORT / DEGRADE

### 不可变与边界
- **不可变模型**: EffectiveRunConfig frozen, ToolMeta frozen, FrozenPromptPrefix frozen
- **iteration_history**: append-only, 不可删除/替换/重排
- **None语义**: None="不存在", 失败用 error 对象, 空集合用 []

## Commands
```bash
pip install -e ".[dev]"           # Install
pytest tests/                     # Tests (1358+ passed)
python -m agent_framework.main    # Interactive (Mock, no API key)
python -m agent_framework.main --config config/deepseek.json      # Real model
python -m agent_framework.main --config config/collection_sequential.json  # Mode A
```

## Config: Collection Strategy
```json
{
  "subagent": {
    "default_collection_strategy": "HYBRID",
    "collection_poll_interval_ms": 500
  }
}
```
Options: `SEQUENTIAL` (逐个, Mode A), `BATCH_ALL` (全部等待, Mode B), `HYBRID` (批次, Mode C 默认)

## Config: LLM Adapters
```json
{"model": {"adapter_type": "openrouter", "api_key": "sk-or-...", "default_model_name": "anthropic/claude-sonnet-4"}}
```
Supported: `openai` `anthropic` `google` `litellm` `openrouter` `together` `groq` `fireworks` `mistral` `perplexity` `deepseek` `doubao` `qwen` `zhipu` `minimax` `siliconflow` `moonshot` `baichuan` `yi` `custom`

## File Conventions
- pydantic v2 BaseModel / pydantic-settings BaseSettings
- TYPE_CHECKING for forward refs, runtime_checkable Protocols
- @tool decorator auto-detects async

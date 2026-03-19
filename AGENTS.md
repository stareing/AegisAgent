# AGENTS.md

本文件定义本仓库协作开发规则。
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
* **代码审核**：由 codex / claude code 排查代码是否和需求一致

## Project Structure

```
agent_framework/
├── agent/           # Agent loop, coordinator, state, prompt templates, skills
├── adapters/model/  # 20 LLM adapters (OpenAI/Anthropic/Google/OpenRouter/Groq/Together/...)
├── context/         # Context engineering, compression, 5-slot builder
├── graph/           # LangGraph-compatible compiled graph engine (StateGraph/CompiledGraph)
├── hooks/           # Hook registry, executor, 3 builtin hooks, payloads
├── infra/           # Config, logging, event bus, tracing (OpenTelemetry)
├── memory/          # Memory manager + 4 store backends (SQLite/PostgreSQL/MongoDB/Neo4j)
├── models/          # Pydantic v2 data models (message/tool/agent/subagent/hook/plugin)
├── plugins/         # Plugin manifest, loader, lifecycle, permissions
├── protocols/       # Core protocols + MCP client + A2A client adapter
├── skills/          # Skill loader, preprocessor, SKILL.md discovery
├── subagent/        # Factory, scheduler, runtime, delegation, HITL, interaction channel, lead collector
├── tools/           # Tool executor, catalog, registry, decorator, confirmation
│   ├── builtin/     # 12 built-in tools (read/write/edit/grep/glob/web/task/spawn/think/skill/shell/memory)
│   ├── schemas/     # Parameter models & validation
│   └── shell/       # BashSession, ShellSessionManager (process isolation)
├── entry.py         # AgentFramework facade
├── cli.py           # CLI entry point
├── main.py          # Interactive terminal
├── terminal_runtime.py  # Rich terminal display
└── textual_cli.py       # Textual TUI
config/              # Model & collection strategy JSON configs (20+ presets)
tests/               # 1358+ tests across 35+ files
```

## Key Subsystems

### 1. Agent Execution Chain
```
CLI → AgentFramework.run() → RunCoordinator.run() → AgentLoop.execute_iteration()
  → LLM call → tool_calls → ToolExecutor.batch_execute() → route by source
  → IterationResult → RunStateController → SessionState → next iteration
```
- RunCoordinator: orchestration (WHEN) | AgentLoop: execution (HOW, zero writes)
- AgentLoop returns immutable IterationResult; only RunStateController writes state.

### 2. Sub-Agent + Long-term Interaction (v3.1)
- **15-state status machine**: PENDING→QUEUED→SCHEDULED→RUNNING→PAUSED/TERMINAL + CANCELLING
- **PauseReason (orthogonal)**: WAIT_PARENT_INPUT / WAIT_USER_INPUT / WAIT_EXTERNAL_EVENT / CHECKPOINT_PAUSE
- **DelegationEvent channel**: append-only, sequence_no monotonic, AckLevel 4-tier (NONE→RECEIVED→PROJECTED→HANDLED)
- **HITL chain**: sub-agent QUESTION → DelegationExecutor → HITLHandler → user → resume_subagent → mark_handled
- **DelegationCapabilities**: A2A agents declare what they support; capability-aware result downgrade on mismatch
- **CheckpointLevel**: NONE / COORDINATION_ONLY / PHASE_RESTARTABLE / STEP_RESUMABLE

### 3. Three Collection Strategies (v3.1)
| Strategy | Behavior | Use When |
|----------|----------|----------|
| **SEQUENTIAL** | Pull 1 completed per call, decision window after each | Dependent tasks, need mid-course correction |
| **BATCH_ALL** | Wait for all (asyncio.gather), return all at once | Independent tasks, only merged result matters |
| **HYBRID** (default) | Pull all currently-completed (≥1) per call | Independent tasks, want early problem detection |

Config: `subagent.default_collection_strategy` / `collection_poll_interval_ms`
LLM override: `spawn_agent(wait=false, collection_strategy="SEQUENTIAL", label="Agent A")`
Collect: `check_spawn_result(batch_pull=true)` → `BatchResult{results, total_spawned, still_running, is_final_batch}`

### 4. LLM Adapters (20 providers)
Native: `openai` `anthropic` `google` | Fallback: `litellm`
International: `openrouter` `together` `groq` `fireworks` `mistral` `perplexity`
Chinese: `deepseek` `doubao` `qwen` `zhipu` `minimax` `siliconflow` `moonshot` `baichuan` `yi`
Generic: `custom` (any OpenAI-compatible endpoint)
All support multimodal (vision) via `ContentPart` → adapter-specific conversion.

## Boundary Rules

### execution_mode vs collection_strategy
- `execution_mode="progressive"`: INTRA-iteration tool result streaming (single LLM turn)
- `collection_strategy`: INTER-iteration spawn result batching (across LLM turns)
- They coexist: LLM explicit `wait=false` enables collection_strategy even in progressive mode

### Sub-agent boundaries
- Sub-agents are delegation objects under parent run's control plane, NOT independent run systems
- Parent LLM only sees DelegationSummary (Layer 2), never raw child sessions
- HITL pending queue belongs to parent run control plane
- cancel is cooperative (CANCELLING → CANCELLED), not instant
- resume_token is a handle, not a full state snapshot; CheckpointLevel declares actual capability

## Testing
```bash
pip install -e ".[dev]"
pytest tests/                     # 1358+ tests
pytest tests/ -k "collection"     # Collection strategy tests (72)
pytest tests/ -k "long_interaction"  # Long-term interaction tests (109)
```

## Config Examples
```bash
python -m agent_framework.main --config config/deepseek.json              # DeepSeek
python -m agent_framework.main --config config/collection_sequential.json  # Mode A
python -m agent_framework.main --config config/collection_batch.json       # Mode B
python -m agent_framework.main --config config/collection_hybrid.json      # Mode C (default)
```

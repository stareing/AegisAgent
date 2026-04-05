# Agent Framework - Project Context

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
* **代码审核**：由 codex / claude 排查代码是否和需求一致

## Project Overview
**Agent Framework** is an offline-first, extensible AI Agent runtime built with Python 3.11+ / pydantic v2. It provides a robust foundation for autonomous agents with multi-model LLM support, local and remote tool execution (MCP), agent-to-agent delegation (A2A), and long-term parent-child interaction.

### Key Features
- **Layered Architecture**: Protocol → Base → Default three-layer pattern for all extensible modules
- **20 LLM Adapters**: OpenAI, Anthropic, Google, OpenRouter, Together, Groq, Fireworks, Mistral, Perplexity, DeepSeek, Doubao, Qwen, Zhipu, MiniMax, SiliconFlow, Moonshot, Baichuan, Yi, LiteLLM, Custom
- **Multimodal**: ContentPart model supports text/image_url/image_base64/audio/file across all adapters
- **Offline-First Memory**: SQLite (default) + PostgreSQL + MongoDB + Neo4j backends
- **Tool Ecosystem**: 12 built-in tools + MCP server integration + A2A remote agent delegation
- **Sub-Agent Runtime**: Factory/Scheduler/Runtime with 15-state status machine, HITL, checkpointing
- **Three Collection Strategies**: SEQUENTIAL (Mode A) / BATCH_ALL (Mode B) / HYBRID (Mode C) for multi-agent orchestration
- **Context Engineering**: 5-slot builder (System → Skills → Memories → History → Input) + LLM compression
- **Graph Engine**: LangGraph-compatible StateGraph/CompiledGraph with conditional routing and fan-out/fan-in
- **1358+ Tests**: Comprehensive coverage across 35+ test modules

### Architecture Layers
```
agent_framework/
├── agent/           # 15 files — Agent loop, coordinator, state, prompt templates
├── adapters/model/  #  8 files — 20 LLM provider adapters + fallback chain
├── context/         #  8 files — Context engineering, compression, prefix cache
├── graph/           #  7 files — LangGraph-compatible compiled graph engine
├── hooks/           # 12 files — Hook system + 3 builtin hooks
├── infra/           #  5 files — Config, logging, event bus, telemetry
├── memory/          #  8 files — Manager + 4 store backends
├── models/          # 12 files — Pydantic v2 data models
├── plugins/         #  7 files — Dynamic plugin system
├── protocols/       #  6 files — Core protocols + MCP + A2A
├── skills/          #  3 files — Skill loading from SKILL.md files
├── subagent/        # 10 files — Sub-agent orchestration + delegation + HITL
├── tools/           # 18 files — Execution engine + 12 builtin tools + shell
├── entry.py         # AgentFramework facade (setup + run + run_stream)
├── cli.py           # CLI entry: agent-cli, agent-interactive
└── main.py          # Interactive REPL terminal
```

## Building and Running

### Prerequisites
- Python 3.11+
- (Optional) API keys for LLM providers via environment variables

### Installation
```bash
pip install -e ".[all,dev]"
```

### Running
```bash
# Interactive mode (mock model, no API key needed)
python -m agent_framework.main

# With real model
python -m agent_framework.main --config config/deepseek.json

# With specific collection strategy
python -m agent_framework.main --config config/collection_sequential.json

# Run tests
pytest tests/
```

## Core Execution Chain

```
User Input
  → AgentFramework.run(task)
    → RunCoordinator.run(agent, deps, task)
      │
      │  while not stop:
      │    ① drain RuntimeNotificationChannel (bg tasks + delegation events)
      │    ② prepare LLM request (context engineering)
      │    ③ AgentLoop.execute_iteration() → LLM call → tool dispatch
      │    ④ RunStateController.apply_iteration_result() (sole write port)
      │    ⑤ track tasks + register background/spawn monitors
      │    ⑥ check stop conditions (6-layer)
      │
    → AgentRunResult(final_answer, usage, artifacts)
```

**Key invariants:**
- AgentLoop is zero-write (returns immutable IterationResult)
- Only RunStateController modifies AgentState/SessionState
- Tool results maintain input order via asyncio.gather

## Sub-Agent Long-term Interaction (v3.1)

### Status Machine (15 states)
```
PENDING → QUEUED → SCHEDULED → RUNNING
RUNNING → WAITING_PARENT | WAITING_USER | SUSPENDED  (paused variants)
PAUSED → RESUMING → RUNNING
RUNNING → CANCELLING → CANCELLED  (cooperative cancel)
RUNNING → COMPLETED | FAILED | TIMEOUT | DEGRADED  (terminal)
```
- PauseReason (orthogonal): WAIT_PARENT_INPUT / WAIT_USER_INPUT / WAIT_EXTERNAL_EVENT / CHECKPOINT_PAUSE
- DegradationReason: READ_ONLY_FALLBACK / NO_INTERACTIVE_SUPPORT / QUOTA_LIMITED / TOOL_UNAVAILABLE

### Event Channel
- DelegationEvent: append-only, per-spawn_id sequence_no strictly monotonic
- AckLevel: NONE → RECEIVED (on drain) → PROJECTED (on inject) → HANDLED (on HITL complete)
- HITL chain: QUESTION event → HITLRequest → handler → HITLResponse → resume_subagent

### Collection Strategies
```json
{"subagent": {"default_collection_strategy": "HYBRID", "collection_poll_interval_ms": 500}}
```
| Strategy | Behavior | Pull count |
|----------|----------|------------|
| SEQUENTIAL | 1 result per pull, decision window after each | N pulls for N agents |
| BATCH_ALL | asyncio.gather all, return everything | 1 pull |
| HYBRID | All currently-completed per pull (≥1) | 1~N pulls (adaptive) |

### Boundary Rules
- Sub-agents are delegation objects, not independent run systems
- Parent only sees DelegationSummary, never raw child sessions
- HITL pending queue belongs to parent run control plane
- A2A agents must declare DelegationCapabilities; unsupported states are downgraded
- execution_mode (intra-iteration) and collection_strategy (inter-iteration) coexist without conflict

## Development Conventions

### Coding Style
- **Type Safety**: Python type hints for all public interfaces
- **Pydantic v2**: All data models inherit from `pydantic.BaseModel`
- **Async First**: I/O-bound operations are `async`
- **Structured Logging**: `structlog` for all logging; no `print()` in framework code

### Architectural Rules
- **Layer Integrity**: Higher layers call lower layers; use Protocols for abstraction
- **Config-driven**: `FrameworkConfig` (pydantic-settings) for all tunable parameters
- **Three-layer pattern**: Protocol → Base → Default for extensible modules

### Testing
- 1358+ tests across 35+ modules
- Matrix tests for collection strategies (72 tests across 3×8 dimensions)
- Architecture guard tests verify invariants (status transitions, error mappings)
- Integration tests use MockModelAdapter (no API keys needed)

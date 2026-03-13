# Aegis Agent Framework

> Offline-first, extensible AI Agent runtime in Python 3.11+ / pydantic v2

An engineering-grade agent framework with clear module boundaries, structured audit trails, and multi-model / multi-protocol support. Designed for single-agent tool-calling, GPT-style saved memory, sub-agent orchestration, and MCP/A2A integration — all runnable offline with a local model.

---

## Quick Start

```bash
# Install (dev mode)
pip install -e ".[dev]"

# Run interactive terminal (mock model, no API key required)
python -m agent_framework.main

# Run with a real model
python -m agent_framework.main --config config/openai.json

# Run demo
python run_demo.py

# Run tests (577 passing)
pytest tests/
```

---

## Features

### Agent Loop
- **ReAct** pattern with final-answer extraction
- **6-layer termination**: LLM_STOP / MAX_ITERATIONS / OUTPUT_TRUNCATED / ERROR / USER_CANCEL / Timeout
- **TerminationKind** classification: NORMAL / ABORT / DEGRADE
- Iteration history as append-only audit trail
- Structured decision models (`StopDecision`, `ToolCallDecision`, `SpawnDecision`) — no bare bools

### Tools
- Built-in: `read_file`, `write_file`, `list_directory`, `run_command`, `spawn_agent`, ...
- Naming convention: `local::<name>` / `mcp::<server>::<name>` / `a2a::<alias>::<name>`
- `@tool` decorator for function-to-tool conversion with auto schema generation
- Parallel execution with serial side-effect commit (`ToolCommitSequencer`)
- Confirmation handler (auto-approve or CLI prompt)
- Capability policy with whitelist intersection semantics

### Context Engineering (5-Slot)

| Slot | Content | Budget |
|------|---------|--------|
| 1 | System Core | 15% |
| 2 | Skill Addon | 5% |
| 3 | Saved Memories | 10% |
| 4 | Session History | 60% |
| 5 | Current Input | 10% |

- Deterministic output (same input = same prompt)
- Sliding-window compression when over token budget
- Read-only contract: context layer never modifies state

### Memory
- SQLite persistence (`data/memories.db`)
- Pattern-based auto-extraction (preferences, constraints, project context)
- Provenance tracking: user / agent / subagent / admin
- Confidence filtering: low-confidence inferred candidates discarded
- Governance: pin, unpin, activate, deactivate, clear

### Multi-Agent Orchestration
- **SubAgentFactory** spawns children with 3 memory scopes: `ISOLATED` / `INHERIT_READ` / `SHARED_WRITE`
- **Scheduler/Runtime separation**: Scheduler handles quota/queuing, Runtime handles execution/lifecycle
- Task state machine: `QUEUED → SCHEDULED → RUNNING → COMPLETED / FAILED / CANCELLED`
- Recursive spawn protection (`allow_spawn_children=False` enforced)
- Unified `SubAgentStatus` for both local and A2A delegation

### Model Adapters (11)

| Adapter | Type |
|---------|------|
| LiteLLM | Unified wrapper |
| OpenAI | Native SDK |
| Anthropic | Native SDK |
| Google GenAI | Native SDK |
| DeepSeek | OpenAI-compatible |
| Doubao (豆包) | OpenAI-compatible |
| Qwen (通义千问) | OpenAI-compatible |
| Zhipu (智谱) | OpenAI-compatible |
| MiniMax | OpenAI-compatible |
| Custom | OpenAI-compatible template |

```bash
# Use different models via config
python -m agent_framework.main --config config/deepseek.json
python -m agent_framework.main --config config/anthropic.json
```

### Protocol Integration
- **MCP**: Client manager for stdio/SSE/HTTP transports, auto tool discovery
- **A2A**: Cross-machine agent RPC with unified error codes
- **Skills**: Declarative skill definitions, trigger keywords, per-skill model overrides

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Entry (entry.py, cli.py, main.py)              │
├─────────────────────────────────────────────────┤
│  Agent Layer                                     │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────┐│
│  │ RunCoordinator│ │RunStateCtrl  │ │PolicyRes.││
│  │ (orchestrate) │ │(sole write)  │ │(config)  ││
│  └──────┬───────┘ └──────────────┘ └──────────┘│
│         │                                        │
│  ┌──────▼───────┐ ┌──────────────┐              │
│  │  AgentLoop   │ │MessageProject│              │
│  │ (iteration)  │ │ (format)     │              │
│  └──────────────┘ └──────────────┘              │
├─────────────────────────────────────────────────┤
│  SubAgent    │  Tools       │  Context  │ Memory│
│  Factory     │  Executor    │  Engineer │ Mgr   │
│  Scheduler   │  Registry    │  Provider │ Store │
│  Runtime     │  Delegation  │  Builder  │ SQLite│
├─────────────────────────────────────────────────┤
│  Adapters (LiteLLM, OpenAI, Anthropic, Google)  │
├─────────────────────────────────────────────────┤
│  Protocols (MCP Client, A2A Client)             │
├─────────────────────────────────────────────────┤
│  Infra (Config, Logger, EventBus, DiskStore)    │
└─────────────────────────────────────────────────┘
```

### Three-Layer Run Coordination

| Layer | Role | Responsibility |
|-------|------|----------------|
| **RunCoordinator** | Orchestrator | WHEN to change state |
| **RunStateController** | State executor | HOW to change state (sole write-port) |
| **RunPolicyResolver** | Config composer | Produces `ResolvedRunPolicyBundle` |

### Key Design Principles
- **Protocol → Base → Default** three-layer pattern for all extensible modules
- **Immutable models**: `EffectiveRunConfig`, `ToolMeta`, `ResolvedRunPolicyBundle` — all frozen
- **Sole write-port**: Only `RunStateController` modifies `AgentState` / `SessionState`
- **Policy interpretation uniqueness**: ContextPolicy → ContextEngineer only, MemoryPolicy → MemoryManager only
- **Observation-only events**: EventBus subscribers must not mutate state
- **No resume**: Interrupted runs are terminal; continuation = new run

---

## Project Structure

```
agent_framework/
├── agent/           # Agent loop, coordinator, state, skills
├── tools/           # Tool decorator, registry, executor, delegation
├── memory/          # Saved memory manager, SQLite store
├── context/         # Context engineering, compression, 5-slot builder
├── subagent/        # Sub-agent factory, scheduler, runtime
├── models/          # Pydantic v2 data models
├── protocols/       # MCP client, A2A client
├── adapters/model/  # LLM adapters (11 providers)
├── infra/           # Config, logging, event bus
├── entry.py         # Framework facade
├── cli.py           # CLI entry point
└── main.py          # Interactive terminal
config/              # Model configuration files (JSON)
tests/               # 577 tests across 10 files
```

---

## Configuration

Config files live in `config/`. Example (`config/openai.json`):

```json
{
  "model": {
    "adapter_type": "openai",
    "model_name": "gpt-4",
    "api_key": "${OPENAI_API_KEY}"
  },
  "context": {
    "max_tokens": 8192
  },
  "memory": {
    "enabled": true,
    "db_path": "data/memories.db"
  }
}
```

Available configs: `openai`, `anthropic`, `google`, `deepseek`, `doubao`, `qwen`, `zhipu`, `minimax`, `custom`.

---

## Custom Tools

```python
from agent_framework.tools.decorator import tool

@tool(name="my_tool", category="general", description="Does something useful")
def my_tool(query: str, limit: int = 10) -> str:
    """Search for something."""
    return f"Found {limit} results for: {query}"
```

Register via `AgentFramework.register_tool(my_tool)` or place in a module and register at startup.

---

## Extending the Framework

### Custom Agent

```python
from agent_framework.agent.base_agent import BaseAgent

class MyAgent(BaseAgent):
    def should_stop(self, iteration_result, agent_state):
        # Custom stop logic — return StopDecision, not bool
        ...

    async def on_tool_call_requested(self, tool_call_request):
        # Custom approval — return ToolCallDecision
        ...
```

### Custom Model Adapter

Implement `ModelAdapterProtocol`:

```python
class MyAdapter:
    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        ...  # → ModelResponse

    async def stream_complete(self, messages, tools=None):
        ...  # → AsyncIterator[ModelChunk]

    def count_tokens(self, messages):
        ...  # → int
```

### Custom Memory Store

Implement `MemoryStoreProtocol` and pass to `DefaultMemoryManager(store=my_store)`.

---

## Testing

```bash
# Full suite (577 tests)
pytest tests/

# Architecture guard tests only
pytest tests/test_architecture_guard.py -v

# Specific module
pytest tests/test_agent.py -v
pytest tests/test_tools.py -v
pytest tests/test_subagent.py -v
```

Test categories:
- **Unit tests**: Agent, tools, memory, context, subagent modules
- **Red-line tests**: 106 architectural boundary assertions (v2.5.2 – v2.6.5)
- **Architecture guard**: 43 anti-bypass scans + fault injection + data flow invariants
- **Integration tests**: Full run lifecycle, model adapter smoke tests

---

## Tech Stack

| Component | Technology |
|-----------|-----------|
| Language | Python 3.11+ |
| Data models | pydantic v2 |
| Configuration | pydantic-settings |
| Logging | structlog |
| Events | blinker |
| LLM routing | litellm |
| Persistence | SQLite |
| Protocols | MCP SDK, A2A SDK |
| Testing | pytest, pytest-asyncio |

---

## Install Options

```bash
# Core only
pip install -e .

# With dev tools
pip install -e ".[dev]"

# With specific adapters
pip install -e ".[openai,anthropic,mcp]"

# Everything
pip install -e ".[all]"
```

---

## License

See [LICENSE](LICENSE) for details.

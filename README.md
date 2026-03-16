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

# Run tests (757 passing)
pytest tests/

# Run with Textual TUI (if installed)
pip install textual
python -m agent_framework.main --config config/deepseek.json
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
- **Frozen prefix**: System identity + skill addon cached as immutable prefix for KV cache reuse
- **XML-structured injection**: `<system-identity>` / `<agent-capabilities>` / `<available-skills>` / `<saved-memories>` boundaries

### Context Management & Token Optimization

The framework supports two session modes. The difference is **what goes into the `messages` array** sent to the API each round.

**Scenario**: User says "Read /tmp/test.txt", model calls `read_file`, then user says "Change it to Hi".

#### `stateless` (default) — full messages every round

```python
# ── Round 1: API receives ─────────────────────────────
messages = [
    {"role": "system",    "content": "<system-identity>...</system-identity>"},
    {"role": "user",      "content": "Read /tmp/test.txt"},
]
# → model calls read_file → returns "Hello World" → answers "The file contains Hello World"

# ── Round 2: API receives ─────────────────────────────
messages = [
    {"role": "system",    "content": "<system-identity>...</system-identity>"},   # ← same system
    {"role": "user",      "content": "Read /tmp/test.txt"},                       # ← repeated
    {"role": "assistant", "tool_calls": [{"id":"tc1", "function":{"name":"read_file",...}}]},  # ← repeated
    {"role": "tool",      "content": "Hello World", "tool_call_id": "tc1"},       # ← repeated
    {"role": "assistant", "content": "The file contains Hello World"},             # ← repeated
    {"role": "user",      "content": "Change it to Hi"},                          # ← new
]
# 6 messages, ~3000 tokens — everything from round 1 is re-sent
```

Every round re-sends: system prompt + all history + current input.
Token count grows linearly. When over budget, **sliding window compression** trims the oldest messages.

#### `stateful` — first round full, subsequent rounds delta only

```python
# ── Round 1: API receives (same as stateless) ────────
messages = [
    {"role": "system",    "content": "<system-identity>...</system-identity>"},
    {"role": "user",      "content": "Read /tmp/test.txt"},
]
# sent_count: 0 → 2

# ── Round 2: API receives (delta only) ────────────────
messages = [
    {"role": "assistant", "tool_calls": [{"id":"tc1", "function":{"name":"read_file",...}}]},  # ← new since last
    {"role": "tool",      "content": "Hello World", "tool_call_id": "tc1"},                   # ← new since last
    {"role": "assistant", "content": "The file contains Hello World"},                         # ← new since last
    {"role": "user",      "content": "Change it to Hi"},                                      # ← new since last
]
# 4 messages, ~200 tokens — no system, no round-1 history
# sent_count: 2 → 6
```

Provider holds the full context server-side. Framework only sends `messages[sent_count:]`.
Compression is **skipped** (trimming would break the sent_count offset).

#### Config

```json
{"model": {"session_mode": "stateless"}}
{"model": {"session_mode": "stateful"}}
```

Default is `stateless`. Only switch to `stateful` if your provider supports server-side conversation state.

#### Under the hood

| Layer | `stateless` | `stateful` |
|-------|-------------|------------|
| **`get_delta_messages()`** | Returns full array | Returns `messages[sent_count:]` |
| **API request size** | Linear growth (all history every round) | Near-constant (only new messages) |
| **Context compression** | Enabled — sliding window trims oldest | Skipped — would break delta offset |
| **`_session.active`** | `False` | `True` |
| **`sent_count`** | Not tracked | Incremented each round |
| **Failure if provider is stateless** | None (always works) | Model loses context (no history in request) |

Implementation chain:
1. `RunCoordinator` calls `adapter.begin_session(run_id)` at run start
2. `ContextEngineer` checks `stateful_session` flag → skips compression if true
3. `AgentLoop._call_llm()` calls `adapter.get_delta_messages(messages)` → sends result to API
4. `adapter.end_session()` called in `finally` block

| | stateless | stateful |
|--|-----------|----------|
| **Round 1** | ~2700 tokens | ~2700 tokens |
| **Round 2** | ~3000 tokens | **~200 tokens** |
| **Round 10** | ~6000 tokens | **~150 tokens** |

> Token numbers are illustrative estimates.

### Skills (SKILL.md)
- File-based skills with YAML frontmatter: `skills/<name>/SKILL.md`
- Progressive disclosure: description in context, full body lazy-loaded on invocation
- `$ARGUMENTS`, `$0`/`$1`, `${SKILL_DIR}`, `!`shell`` preprocessing
- LLM invokes via `invoke_skill` tool based on semantic description matching

### Orchestrator
- **OrchestratorAgent**: coordination-aware prompt, parallel/sequential delegation
- Dynamic capability injection: `<agent-capabilities>` with live iteration/spawn counts
- Hard exit guard: forces stop after 3 post-spawn iterations without synthesis
- Sub-agent cleanup on run exit

### Memory & Storage Backends

Multi-backend persistence via `MemoryStoreProtocol`:

| Backend | Config `store_type` | Dependency | Status |
|---------|-------------------|------------|--------|
| **SQLite** | `sqlite` (default) | built-in | Production |
| **PostgreSQL** | `postgresql` | `psycopg2-binary` | Production |
| **MongoDB** | `mongodb` | `pymongo` | Production |
| **Neo4j** | `neo4j` | `neo4j` | Production |

```json
// SQLite (default)
{"memory": {"db_path": "data/memories.db"}}

// PostgreSQL
{"memory": {"store_type": "postgresql", "connection_url": "postgresql://user:pass@host/db"}}

// MongoDB
{"memory": {"store_type": "mongodb", "connection_url": "mongodb://host:27017", "database_name": "agent_db"}}

// Neo4j
{"memory": {"store_type": "neo4j", "connection_url": "bolt://host:7687", "neo4j_auth": "neo4j:pass"}}
```

Features (all backends):
- Pattern-based auto-extraction (preferences, constraints, project context)
- Provenance tracking: user / agent / subagent / admin
- Confidence filtering: low-confidence inferred candidates discarded
- Governance: pin, unpin, activate, deactivate, clear
- Conversation history persistence with multi-session support

### Conversation History Persistence
- SQLite-backed (`data/memories.db`, shared with memory store)
- **Project-scoped**: uses `cwd` folder name (e.g. `my-agent`) as unique project ID
- **Multi-session**: each `/reset` creates a new conversation window; old ones preserved
- Auto-save on exit, auto-restore on startup (with last 3 turns summary)
- Commands: `/sessions` (list all), `/session-switch <id>` (switch), `/reset` (new window), `/history-clear` (delete current)

### Interactive Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all available commands |
| `/reset` | Save current & open new context window |
| `/sessions` | List all conversation sessions for this project |
| `/session-switch <id>` | Switch to a different session (prefix match) |
| `/history` | View conversation history |
| `/history-clear` | Clear current session (memory + DB) |
| `/tools` | List registered tools |
| `/skills` | List available skills |
| `/config` | Show current configuration |
| `/stats` | Show context token statistics |
| `/compact` | LLM-based history compression |
| `/exit` | Save & exit |

### Multi-Agent Orchestration
- **SubAgentFactory** spawns children with 3 memory scopes: `ISOLATED` / `INHERIT_READ` / `SHARED_WRITE`
- **Scheduler/Runtime separation**: Scheduler handles quota/queuing, Runtime handles execution/lifecycle
- Task state machine: `QUEUED → SCHEDULED → RUNNING → COMPLETED / FAILED / CANCELLED / TIMEOUT`
- Recursive spawn protection (`allow_spawn_children=False` enforced)
- Unified `SubAgentStatus` for both local and A2A delegation

#### Execution Modes

| Mode | Config | Behavior |
|------|--------|----------|
| **parallel** (default) | `"execution_mode": "parallel"` | Wait for all tools to complete, return results together |
| **progressive** | `"execution_mode": "progressive"` | Return each result as it completes (fastest first) |

```json
{
  "subagent": {
    "execution_mode": "progressive"
  }
}
```

Progressive mode: LLM spawns 3 sub-agents → all run in parallel → as each finishes, its result is immediately returned to the LLM → LLM processes incrementally → final summary after all complete.

#### Capability Plane Architecture

All Agent-facing tools (local, MCP, A2A, subagent, memory_admin) route through `ToolExecutor`:
- Main agent loops call `ToolExecutor.batch_execute()` or `batch_execute_progressive()`
- Those executor entrypoints then funnel each tool call through `ToolExecutor.execute()`

This unified tool execution plane enforces:
- **Capability policy** (`CapabilityPolicy` whitelist/blacklist)
- **Confirmation handler** (auto-approve or CLI prompt)
- **Error envelope** (structured `ToolResult` + `ToolExecutionError`)
- **Audit trail** (structlog events with timing/source)

Admin-plane methods on `AgentFramework` (list_memories, clear_memories, etc.) are separate — they bypass ToolExecutor intentionally for host application use.

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

### Protocol Integration — MCP

Full [Model Context Protocol](https://modelcontextprotocol.io/) client support:

| Capability | API |
|-----------|-----|
| Tools | `call_mcp_tool()` — discover & call remote tools |
| Resources | `list_resources()` / `read_resource(uri)` |
| Resource Templates | `list_resource_templates()` |
| Prompts | `list_prompts()` / `get_prompt(name, args)` |
| Sampling | `set_sampling_callback()` — LLM requests from server |
| Transports | stdio / SSE / streamable HTTP |

Config (`config/*.json`):
```json
{
  "mcp": {
    "servers": [
      {
        "server_id": "my-tools",
        "transport": "stdio",
        "command": "python",
        "args": ["tests/mcp_test_server.py"]
      },
      {
        "server_id": "remote",
        "transport": "sse",
        "url": "http://localhost:8080/sse"
      }
    ]
  }
}
```

### Protocol Integration — A2A

Full [Agent-to-Agent](https://google.github.io/A2A/) protocol support:

| Capability | API |
|-----------|-----|
| Discovery | `discover_agent(url, alias)` — fetch agent card |
| Delegation | `delegate_task(alias, input)` — send task, get result |
| Streaming | `delegate_task_streaming(alias, input)` — stream events |
| Task Mgmt | `get_task()` / `cancel_task()` / `resubscribe()` |
| Server | `build_a2a_server()` — expose local agent as A2A server |

Config (`config/*.json`):
```json
{
  "a2a": {
    "known_agents": [
      {"url": "http://localhost:9100", "alias": "echo"}
    ]
  }
}
```

Expose local agent as A2A server:
```python
app = framework.build_a2a_server(name="my-agent", port=9000)
uvicorn.run(app, host="0.0.0.0", port=9000)
```

### Skills
- Declarative skill definitions, trigger keywords, per-skill model overrides

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
tests/               # 757 tests across 10+ files
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

### Programmatic Usage (Secondary Development)

```python
import asyncio
from agent_framework.entry import AgentFramework
from agent_framework.infra.config import load_config
from agent_framework.tools.decorator import tool

# 1. Define custom tools
@tool(name="search", description="Search the knowledge base")
def search(query: str) -> str:
    return f"Results for: {query}"

# 2. Load config & setup
config = load_config("config/deepseek.json")
fw = AgentFramework(config=config)
fw.setup(auto_approve_tools=True)
fw.register_tool(search)

# 3. Single run
async def main():
    result = await fw.run("What is Python?")
    print(result.final_answer)

    # Multi-turn with history
    result1 = await fw.run("Read /tmp/test.txt")
    result2 = await fw.run(
        "Summarize it",
        initial_session_messages=result1.session_messages,
    )
    print(result2.final_answer)
    await fw.shutdown()

asyncio.run(main())
```

### Streaming Output

```python
async for event in fw.run_stream("Explain async in Python"):
    if event.type.name == "TOKEN":
        print(event.data["token"], end="", flush=True)
    elif event.type.name == "DONE":
        result = event.data["result"]
```

### Custom Skill (File-based)

Create `skills/my-skill/SKILL.md`:

```markdown
---
name: my-skill
description: Analyzes code quality
allowed-tools: [read_file, list_directory]
---

Analyze the code in $ARGUMENTS for quality issues.
Focus on: naming, complexity, error handling.
```

Then: `/skill my-skill src/main.py` in the interactive terminal, or register programmatically:

```python
fw.register_skill(Skill(
    skill_id="my-skill",
    name="Code Quality",
    description="Analyzes code quality",
    system_prompt_addon="You are a code quality reviewer...",
))
```

### Embedding in Web Applications

```python
from fastapi import FastAPI
from agent_framework.entry import AgentFramework

app = FastAPI()
fw = AgentFramework(config=load_config("config/openai.json"))
fw.setup(auto_approve_tools=True)

@app.post("/chat")
async def chat(message: str, session_id: str | None = None):
    # Load prior messages from your session store
    prior_messages = load_session(session_id) if session_id else []
    result = await fw.run(
        message,
        initial_session_messages=prior_messages,
        user_id="web-user",
    )
    # Save updated messages to your session store
    save_session(session_id, result.session_messages)
    return {"answer": result.final_answer}
```

### Key Extension Points

| Extension | Protocol/Base | How to Inject |
|-----------|---------------|---------------|
| Agent behavior | `BaseAgent` | `fw.setup(agent=MyAgent(...))` |
| Model provider | `ModelAdapterProtocol` | `fw._deps.model_adapter = MyAdapter()` |
| Memory storage | `MemoryStoreProtocol` | `DefaultMemoryManager(store=...)` |
| Tools | `@tool` decorator | `fw.register_tool(fn)` |
| Skills | `Skill` model | `fw.register_skill(skill)` |
| MCP servers | Config JSON | `fw.config.mcp.servers` |

---

## Testing

```bash
# Full suite (757 tests)
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

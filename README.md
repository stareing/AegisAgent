# Aegis Agent Framework

> Offline-first, extensible AI Agent runtime in Python 3.11+ / pydantic v2

An engineering-grade agent framework with clear module boundaries, structured audit trails, and multi-model / multi-protocol support. Designed for single-agent tool-calling, GPT-style saved memory, sub-agent orchestration, **LangGraph-compatible compiled graph workflows**, and MCP/A2A integration — all runnable offline with a local model.

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

# Run tests (1120 passing)
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
- **8 categories**: filesystem, code, system, network, delegation, control, memory_admin, reasoning
- **Sub-agent safety**: `SUBAGENT_SAFE` / `SUBAGENT_BLOCKED` / `HIGH_RISK` category sets enforce tool isolation

### Tool Categories & Security

| Category | Examples | Sub-agent Access |
|----------|----------|-----------------|
| `filesystem` | read_file, write_file, list_directory, grep_search | Allowed |
| `code` | code_edit | Allowed |
| `reasoning` | think | Allowed |
| `system` | get_system_info, get_env | Blocked |
| `network` | web_search, web_fetch | Blocked |
| `delegation` | spawn_agent | Blocked |
| `control` | slash_command, exit_plan_mode | Blocked |
| `memory_admin` | remember, forget, list_memories | Blocked |

### Context Engineering (5-Slot)

| Slot | Content | Budget |
|------|---------|--------|
| 1 | System Core | 15% |
| 2 | Skill Addon | 5% |
| 3 | Saved Memories | 10% |
| 4 | Session History | 60% |
| 5 | Current Input | 10% |

- Deterministic output (same input = same prompt)
- LLM incremental summarization for context compression
- Read-only contract: context layer never modifies state
- **Frozen prefix**: System identity + skill addon cached as immutable prefix for KV cache reuse
- **XML-structured injection**: `<system-identity>` / `<agent-capabilities>` / `<available-skills>` / `<saved-memories>` boundaries
- **Multimodal support**: `ContentPart` with image_url/base64, compression protection for multimodal groups

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
Token count grows linearly. When over budget, **LLM summarization** trims the oldest messages.

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
| **Context compression** | Enabled — LLM summarization trims oldest | Skipped — would break delta offset |
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
- `$ARGUMENTS`, `$0`/`$1`, `${SKILL_DIR}`, `` !`shell` `` preprocessing
- LLM invokes via `invoke_skill` tool based on semantic description matching

### Orchestrator
- **OrchestratorAgent**: coordination-aware prompt, parallel/sequential delegation
- Dynamic capability injection: `<agent-capabilities>` with live iteration/spawn counts
- Hard exit guard: forces stop after N post-spawn iterations without synthesis (configurable per-instance)
- O(1) spawn tracking via `AgentState.last_spawn_iteration_index`
- Sub-agent cleanup on run exit

---

## Graph Workflows (LangGraph-Compatible)

The framework provides a **compiled graph execution engine** with an API surface that mirrors [LangGraph](https://github.com/langchain-ai/langgraph), enabling declarative DAG-based workflows alongside the ReAct agent loop.

### Quick Example

```python
import operator
from typing import Annotated
from typing_extensions import TypedDict
from agent_framework import StateGraph, START, END

class State(TypedDict):
    messages: Annotated[list[str], operator.add]  # reducer: append
    count: int                                     # last-write-wins

def greet(state: State) -> dict:
    return {"messages": ["Hello!"], "count": state["count"] + 1}

def farewell(state: State) -> dict:
    return {"messages": ["Goodbye!"], "count": state["count"] + 1}

# Build
graph = StateGraph(State)
graph.add_node("greet", greet)
graph.add_node("farewell", farewell)
graph.add_edge(START, "greet")
graph.add_edge("greet", "farewell")
graph.add_edge("farewell", END)

# Compile & run
app = graph.compile()
result = await app.invoke({"messages": [], "count": 0})
# {'messages': ['Hello!', 'Goodbye!'], 'count': 2}
```

### State & Reducers

State is defined as a `TypedDict`. Each field can optionally have a **reducer** via `Annotated`:

```python
from typing import Annotated
import operator

class MyState(TypedDict):
    items: Annotated[list[str], operator.add]       # append lists
    total: Annotated[int, lambda old, new: old + new]  # sum accumulator
    label: str                                        # last-write-wins (default)
```

- Nodes return **partial dicts** — only the fields they want to update
- Reducers merge old + new values; plain fields use last-write-wins
- State is deep-copied before each node — nodes cannot corrupt shared state

### Conditional Routing

```python
def router(state: State) -> str:
    if state["count"] >= 3:
        return "summarize"
    return "process"

graph.add_conditional_edges(
    "check",                  # source node
    router,                   # router function → returns branch key
    {                         # mapping: branch key → target node
        "process": "process",
        "summarize": "summarize",
    },
)
```

Routers can also return node names directly (no mapping needed), or return `END` to terminate:

```python
graph.add_conditional_edges("loop", lambda s: END if s["done"] else "loop")
```

### Fan-out / Fan-in (Parallel Branches)

Multiple edges from the same source node execute targets **concurrently** via `asyncio.gather`:

```python
graph.add_edge("a", "b")   # b and c run in parallel
graph.add_edge("a", "c")   # after a completes
graph.add_edge("b", "d")   # d waits for both b and c
graph.add_edge("c", "d")
```

Use `Annotated` reducers to merge parallel results:

```python
class State(TypedDict):
    results: Annotated[list[str], operator.add]  # parallel branches append
```

### Streaming

Three stream modes for incremental observation:

```python
# Full state after each node
async for event in app.stream(input_state, stream_mode="values"):
    print(f"[{event.node}] state = {event.data}")

# Partial update from each node
async for event in app.stream(input_state, stream_mode="updates"):
    print(f"[{event.node}] update = {event.data}")

# Debug: state + update + timing
async for event in app.stream(input_state, stream_mode="debug"):
    print(f"[{event.node}] {event.data['duration_ms']:.1f}ms")
```

### Checkpointing & Persistence

Enable state persistence for resumable workflows:

```python
from agent_framework import InMemorySaver

# In-memory (for dev/test)
app = graph.compile(checkpointer=InMemorySaver())

# Thread-based state isolation
config = {"configurable": {"thread_id": "user-123"}}
result1 = await app.invoke({"messages": ["Hi"]}, config)
result2 = await app.invoke({"messages": ["Continue"]}, config)  # resumes from checkpoint
```

Implement `CheckpointerProtocol` for custom persistence (Redis, SQLite, etc.):

```python
from agent_framework import CheckpointerProtocol

class RedisCheckpointer(CheckpointerProtocol):
    async def save(self, thread_id: str, state: dict, node: str, step: int) -> None:
        await redis.set(f"graph:{thread_id}", json.dumps(state))

    async def load(self, thread_id: str) -> dict | None:
        data = await redis.get(f"graph:{thread_id}")
        return json.loads(data) if data else None
```

### Agent Node Integration

Embed full agent runs as graph nodes:

```python
from agent_framework import AgentFramework, StateGraph, START, END, agent_node

fw = AgentFramework(config=load_config("config/deepseek.json"))
fw.setup(auto_approve_tools=True)

graph = StateGraph(PipelineState)
graph.add_node("research", agent_node(fw, task_key="query", output_key="research_result"))
graph.add_node("summarize", agent_node(fw, task_key="summarize_prompt", output_key="summary"))
graph.add_edge(START, "research")
graph.add_edge("research", "summarize")
graph.add_edge("summarize", END)
app = graph.compile()
```

### Node Helpers

```python
from agent_framework import tool_node, passthrough_node, branch_node

# Wrap a plain function as a node
graph.add_node("double", tool_node(lambda x: x * 2, input_key="value", output_key="result"))

# No-op passthrough (routing-only node)
graph.add_node("gate", passthrough_node())

# Router helper (documentation sugar)
router = branch_node(lambda s: "a" if s["flag"] else "b")
graph.add_conditional_edges("gate", router, {"a": "node_a", "b": "node_b"})
```

### Batch Execution

Run multiple inputs concurrently:

```python
results = await app.abatch([
    {"query": "What is Python?"},
    {"query": "What is Rust?"},
    {"query": "What is Go?"},
])
```

### Introspection

```python
structure = app.get_graph_structure()
# {
#   "name": "MyGraph",
#   "nodes": ["research", "summarize"],
#   "edges": [{"source": "__start__", "target": "research"}, ...],
#   "conditional_edges": [...]
# }
```

### Compile-Time Validation

`compile()` validates the graph topology:
- All nodes must be reachable from `START`
- All nodes must have a path to `END`
- Unreachable or dead-end nodes raise `UnreachableNodeError` / `NoPathToEndError`
- Recursion limit (default 25) prevents infinite loops at runtime

### Graph API Reference

| Builder | Description |
|---------|-------------|
| `StateGraph(State)` | Create a new graph builder with a TypedDict state schema |
| `.add_node(name, fn)` | Register a sync or async node function |
| `.add_node(fn)` | Register using `fn.__name__` as node name |
| `.add_edge(src, dst)` | Add a direct edge between nodes |
| `.add_conditional_edges(src, router, mapping)` | Add conditional routing |
| `.set_entry_point(name)` | Alias for `add_edge(START, name)` |
| `.set_finish_point(name)` | Alias for `add_edge(name, END)` |
| `.compile(**kwargs)` | Validate and produce a `CompiledGraph` |

| Compiled | Description |
|----------|-------------|
| `await app.invoke(state, config)` | Run to completion, return final state |
| `async for e in app.stream(state, config, stream_mode=)` | Yield per-node events |
| `await app.abatch(inputs, configs)` | Concurrent multi-input execution |
| `app.get_graph_structure()` | Serializable topology dict |

| Compile Options | Description |
|----------------|-------------|
| `checkpointer=` | Persistence backend for state snapshots |
| `name=` | Human-readable graph name |
| `recursion_limit=` | Max node invocations per run (default 25) |

---

## Memory & Storage Backends

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

---

## Multi-Agent Orchestration

- **SubAgentFactory** spawns children with 3 memory scopes: `ISOLATED` / `INHERIT_READ` / `SHARED_WRITE`
- **Scheduler/Runtime separation**: Scheduler handles quota/queuing, Runtime handles execution/lifecycle
- Task state machine: `QUEUED → SCHEDULED → RUNNING → COMPLETED / FAILED / CANCELLED / TIMEOUT`
- Recursive spawn protection (`allow_spawn_children=False` enforced)
- Unified `SubAgentStatus` for both local and A2A delegation

### Execution Modes

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

Progressive mode works for **all parallel tool calls** (not just spawn_agent): LLM dispatches multiple tools → all run in parallel → as each finishes, its result is immediately returned to the LLM → LLM responds incrementally → final summary after all complete. In TUI, spawn_agent shows as `[subagent N/M]`, other tools as `[tool N/M]`.

### Capability Plane Architecture

All Agent-facing tools (local, MCP, A2A, subagent, memory_admin) route through `ToolExecutor`:
- Main agent loops call `ToolExecutor.batch_execute()` or `batch_execute_progressive()`
- Those executor entrypoints then funnel each tool call through `ToolExecutor.execute()`

This unified tool execution plane enforces:
- **Capability policy** (`CapabilityPolicy` whitelist/blacklist)
- **Confirmation handler** (auto-approve or CLI prompt)
- **Error envelope** (structured `ToolResult` + `ToolExecutionError`)
- **Audit trail** (structlog events with timing/source)

Admin-plane methods on `AgentFramework` (list_memories, clear_memories, etc.) are separate — they bypass ToolExecutor intentionally for host application use.

---

## Model Adapters (11) + Fallback Chain

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

All adapters have built-in exponential backoff retry (`2^attempt + random`, capped at 30s). **Fallback chain** support: when primary model retries are exhausted, automatically tries backup models in order.

```json
{
  "model": {
    "adapter_type": "doubao",
    "default_model_name": "doubao-seed-2-0-pro",
    "fallback_models": [
      {"adapter_type": "deepseek", "default_model_name": "deepseek-chat", "api_key": "..."},
      {"adapter_type": "openai", "default_model_name": "gpt-4o", "api_key": "..."}
    ]
  }
}
```

```bash
# Use different models via config
python -m agent_framework.main --config config/deepseek.json
python -m agent_framework.main --config config/anthropic.json
```

---

## Protocol Integration — MCP

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

## Protocol Integration — A2A

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

---

## Security Sandbox

- **Shell tools disabled by default**: `tools.shell_enabled: false`, must be explicitly enabled
- **Environment variable whitelist**: child processes inherit only safe vars (`PATH`, `HOME`, `PYTHONPATH`, etc.), API keys and secrets are filtered out
- **Command blocklist**: 26 high-risk commands (`sudo`, `curl`, `wget`, `iptables`, etc.) blocked
- **Filesystem sandbox**: `AGENT_FS_SANDBOX_ROOTS` restricts file access scope; sensitive paths (`.env`, `.pem`, `.ssh`) auto-blocked
- **`get_env` requires confirmation**: reading environment variables needs user approval
- **Shell module isolation**: `BashSession` and `ShellSessionManager` extracted to `tools/shell/` for security review separation

---

## Observability (OpenTelemetry)

Native OpenTelemetry distributed tracing. Automatically falls back to zero-cost noop when SDK is absent.

```
agent.run (run_id, agent_id, model, task)
├── agent.iteration (iteration_index, context_messages)
│   ├── agent.llm.call (message_count)
│   └── agent.tool (tool_name, source, success)
└── [success, iterations_used, total_tokens, elapsed_ms]
```

```json
{
  "tracing": {
    "enabled": true,
    "exporter_type": "console",
    "service_name": "my-agent"
  }
}
```

Supports OTLP export to Jaeger / Grafana Tempo. Install optional deps: `pip install -e ".[otel]"`

---

## Hooks & Plugins Extension System

Governed extension points — hooks observe, gate, or modify at 20 predefined lifecycle points. Plugins package hooks, tools, commands, and agent templates as installable units. All hook dispatch goes through a unified `HookDispatchService`; plugins use a symmetric `PluginExtensionRegistrar` for atomic apply/rollback.

### Hook Points (20)

| Category | Points | DENY | MODIFY |
|----------|--------|------|--------|
| **Run** | `run.start`, `run.finish`, `run.error` | No | No |
| **Iteration** | `iteration.start`, `iteration.finish`, `iteration.error` | No | No |
| **Tool** | `tool.pre_use`, `tool.post_use`, `tool.error` | Pre | Pre: `sanitized_arguments`, `display_name` |
| **Delegation** | `delegation.pre`, `delegation.post`, `delegation.error` | Pre | Pre: `task_input_override`, `deadline_ms_override` |
| **Memory** | `memory.pre_record`, `memory.post_record` | Pre | Pre: `content`, `title`, `tags` |
| **Context** | `context.pre_build`, `context.post_build` | Pre | Pre: `extra_instructions`, `compression_preference` |
| **Artifact** | `artifact.produced`, `artifact.finalize` | No | No |
| **Config** | `config.loaded`, `instructions.loaded` | No | No |

### Writing a Custom Hook

```python
from agent_framework import (
    HookCategory, HookContext, HookExecutionMode, HookFailurePolicy,
    HookMeta, HookPoint, HookResult, HookResultAction,
)

class MyToolGuard:
    """Block tools with oversized arguments."""

    def __init__(self) -> None:
        self._meta = HookMeta(
            hook_id="my_org.tool_guard",
            plugin_id="my_org",
            name="Argument Size Guard",
            hook_point=HookPoint.PRE_TOOL_USE,
            category=HookCategory.COMMAND,
            failure_policy=HookFailurePolicy.WARN,
            priority=10,
            timeout_ms=1000,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        args = context.payload.get("arguments", {})
        if len(str(args)) > 50_000:
            return HookResult(
                action=HookResultAction.DENY,
                message="Arguments too large",
            )
        return HookResult(action=HookResultAction.ALLOW)

# Register with framework
framework.register_hook(MyToolGuard())
```

Async hooks are also supported — just make `execute` an `async def`.

### Hook Result Actions

| Action | Meaning | Where valid |
|--------|---------|-------------|
| `ALLOW` / `NOOP` | Pass through | All points |
| `DENY` | Block the operation | Pre-use points only (`tool.pre_use`, `delegation.pre`, `memory.pre_record`, `context.pre_build`) |
| `MODIFY` | Change whitelisted payload fields | Pre-use points (per-point field whitelist enforced) |
| `REQUEST_CONFIRMATION` | Trigger user confirmation handler | Pre-use points |
| `EMIT_ARTIFACT` | Produce artifacts for registration | Post-use points |

### Built-in Hooks

| Hook | Point | Purpose |
|------|-------|---------|
| `ToolGuardHook` | `tool.pre_use` | Argument size limit, dangerous tag filtering |
| `AuditNotifyHook` | `run.finish` / `run.error` | Structured audit records + notification callback |
| `MemoryReviewHook` | `memory.pre_record` | Content policy (size, tags, sensitive data detection) |

```python
from agent_framework.hooks.builtin import ToolGuardHook, AuditNotifyHook, MemoryReviewHook

framework.register_hook(ToolGuardHook(max_argument_chars=50_000))
framework.register_hook(AuditNotifyHook(
    hook_point=HookPoint.RUN_FINISH,
    notify_callback=lambda record: requests.post(WEBHOOK_URL, json=record),
))
framework.register_hook(MemoryReviewHook(max_content_length=5000))
```

### Plugin System

Plugins are installable extension packages providing hooks, tools, commands (skills), and agent templates:

```python
from agent_framework import PluginManifest, PluginPermission

class MyPlugin:
    @property
    def manifest(self) -> PluginManifest:
        return PluginManifest(
            plugin_id="my-plugin",
            name="My Plugin",
            version="1.0.0",
            provides_hooks=True,
            provides_tools=True,
            provides_commands=True,
            required_permissions=[PluginPermission.REGISTER_HOOKS],
        )

    def load(self) -> None: ...
    def enable(self) -> None: ...
    def disable(self) -> None: ...
    def unload(self) -> None: ...

    def get_hooks(self) -> list:
        return [MyToolGuard()]

    def get_tools(self) -> list:
        return []  # ToolEntry list

    def get_commands(self) -> list:
        return []  # Skill list

    def get_agents(self) -> list:
        return []  # Agent template dicts

framework.load_plugin(MyPlugin())
framework.enable_plugin("my-plugin")
framework.disable_plugin("my-plugin")
```

- **Lifecycle**: `DISCOVERED → VALIDATED → LOADED → ENABLED ⇄ DISABLED → UNLOADED`
- **Mandatory validation**: `enable()` always runs `validate()` first (permissions, dependencies, framework version range)
- **Permission model**: 10 declarative permissions; `REGISTER_TOOLS` and `SPAWN_AGENT` are high-risk (require explicit grant)
- **Conflict detection**: bidirectional — `A.conflicts = ["B"]` blocks both `A→B` and `B→A`
- **Atomic rollback**: `enable()` failure reverts all partial registrations (hooks, tools, commands) via `PluginExtensionRegistrar`
- **Symmetric disable**: `disable()` removes exactly what `enable()` registered — nothing leaks

---

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Entry (entry.py, cli.py, main.py)                  │
├─────────────────────────────────────────────────────┤
│  Agent Layer                                         │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────┐    │
│  │ RunCoordinator│ │RunStateCtrl  │ │PolicyRes.│    │
│  │ (orchestrate) │ │(sole write)  │ │(config)  │    │
│  └──────┬───────┘ └──────────────┘ └──────────┘    │
│         │                                            │
│  ┌──────▼───────┐ ┌──────────────┐                  │
│  │  AgentLoop   │ │MessageProject│                  │
│  │ (iteration)  │ │ (format)     │                  │
│  └──────────────┘ └──────────────┘                  │
├─────────────────────────────────────────────────────┤
│  Graph Engine (StateGraph → CompiledGraph)           │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────┐    │
│  │ StateGraph   │ │CompiledGraph │ │Checkpoint│    │
│  │ (builder)    │ │(executor)    │ │(persist) │    │
│  └──────────────┘ └──────────────┘ └──────────┘    │
├─────────────────────────────────────────────────────┤
│  SubAgent    │  Tools       │  Context  │ Memory    │
│  Factory     │  Executor    │  Engineer │ Mgr       │
│  Scheduler   │  Registry    │  Provider │ Store     │
│  Runtime     │  Delegation  │  Builder  │ SQLite    │
├─────────────────────────────────────────────────────┤
│  Adapters (LiteLLM, OpenAI, Anthropic, Google, ...) │
├─────────────────────────────────────────────────────┤
│  Protocols (MCP Client, A2A Client)                 │
├─────────────────────────────────────────────────────┤
│  Hooks (Registry, Executor, Builtin Hooks)          │
│  Plugins (Manifest, Loader, Lifecycle, Perms)       │
├─────────────────────────────────────────────────────┤
│  Infra (Config, Logger, EventBus, Telemetry)        │
└─────────────────────────────────────────────────────┘
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
- **Hook governance**: Hooks consume DTO snapshots only, cannot bypass CommitSequencer or write core state
- **No resume**: Interrupted runs are terminal; continuation = new run

---

## Project Structure

```
agent_framework/
├── agent/           # Agent loop, coordinator, state, skills
├── graph/           # Compiled graph engine (StateGraph, CompiledGraph)
├── tools/           # Tool decorator, registry, executor, delegation
│   ├── builtin/     # Built-in tools (8 categories)
│   ├── schemas/     # Parameter models & ToolCategory constants
│   └── shell/       # BashSession, ShellSessionManager (isolated)
├── memory/          # Saved memory manager, SQLite store
├── context/         # Context engineering, compression, 5-slot builder
├── subagent/        # Sub-agent factory, scheduler, runtime
├── hooks/           # Hook registry, executor, builtin hooks
├── plugins/         # Plugin manifest, loader, lifecycle, permissions
├── models/          # Pydantic v2 data models (incl. hook & plugin)
├── protocols/       # MCP client, A2A client
├── adapters/model/  # LLM adapters (11 providers)
├── infra/           # Config, logging, event bus, tracing
├── entry.py         # Framework facade
├── cli.py           # CLI entry point
└── main.py          # Interactive terminal
config/              # Model configuration files (JSON)
tests/               # 1120 tests across 29 files
```

---

## Top-Level Imports

The framework exports 80+ symbols from `agent_framework` for convenience:

```python
# Core
from agent_framework import AgentFramework, FrameworkConfig, load_config

# Agents
from agent_framework import BaseAgent, DefaultAgent, OrchestratorAgent

# Tool creation
from agent_framework import tool

# Graph workflows
from agent_framework import StateGraph, CompiledGraph, START, END, InMemorySaver
from agent_framework import agent_node, tool_node, passthrough_node, StreamMode

# Models
from agent_framework import Message, AgentRunResult, Skill, StopSignal, TerminationKind
from agent_framework import CapabilityPolicy, MemoryPolicy, ContextPolicy

# Streaming
from agent_framework import StreamEvent, StreamEventType

# Protocols (for custom implementations)
from agent_framework import ModelAdapterProtocol, ToolExecutorProtocol, MemoryManagerProtocol
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
from agent_framework import tool

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
from agent_framework import BaseAgent, StopDecision, ToolCallDecision

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
from agent_framework import ModelAdapterProtocol

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
from agent_framework import AgentFramework, load_config, tool

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
from agent_framework import StreamEventType

async for event in fw.run_stream("Explain async in Python"):
    if event.type == StreamEventType.TOKEN:
        print(event.data["token"], end="", flush=True)
    elif event.type == StreamEventType.DONE:
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
from agent_framework import Skill

fw.register_skill(Skill(
    skill_id="my-skill",
    name="Code Quality",
    description="Analyzes code quality",
    system_prompt_addon="You are a code quality reviewer...",
))
```

### Graph Workflow Example

```python
import asyncio
import operator
from typing import Annotated
from typing_extensions import TypedDict
from agent_framework import StateGraph, START, END, InMemorySaver

class PipelineState(TypedDict):
    data: Annotated[list[str], operator.add]
    stage: str

def extract(state):
    return {"data": ["extracted_data"], "stage": "transform"}

def transform(state):
    return {"data": [f"transformed({d})" for d in state["data"]], "stage": "load"}

def load(state):
    return {"data": [f"loaded({d})" for d in state["data"]], "stage": "done"}

graph = StateGraph(PipelineState)
graph.add_node("extract", extract)
graph.add_node("transform", transform)
graph.add_node("load", load)
graph.add_edge(START, "extract")
graph.add_edge("extract", "transform")
graph.add_edge("transform", "load")
graph.add_edge("load", END)

app = graph.compile(checkpointer=InMemorySaver(), name="ETL-Pipeline")

async def main():
    result = await app.invoke({"data": [], "stage": "start"})
    print(result)
    # {'data': ['extracted_data', 'transformed(extracted_data)', 'loaded(transformed(extracted_data))'],
    #  'stage': 'done'}

asyncio.run(main())
```

### Embedding in Web Applications

```python
from fastapi import FastAPI
from agent_framework import AgentFramework, load_config

app = FastAPI()
fw = AgentFramework(config=load_config("config/openai.json"))
fw.setup(auto_approve_tools=True)

@app.post("/chat")
async def chat(message: str, session_id: str | None = None):
    prior_messages = load_session(session_id) if session_id else []
    result = await fw.run(
        message,
        initial_session_messages=prior_messages,
        user_id="web-user",
    )
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
| Graph workflow | `StateGraph` | `graph.compile().invoke(state)` |
| MCP servers | Config JSON | `fw.config.mcp.servers` |

---

## Testing

```bash
# Full suite (1120 tests)
pytest tests/

# Architecture guard tests only
pytest tests/test_architecture_guard.py -v

# Graph tests
pytest tests/test_graph.py -v

# Specific module
pytest tests/test_agent.py -v
pytest tests/test_tools.py -v
pytest tests/test_subagent.py -v
```

Test categories:
- **Unit tests**: Agent, tools, memory, context, subagent, graph modules (~400)
- **Red-line tests**: 106 architectural boundary assertions (v2.5.2 – v2.6.5)
- **Architecture guard**: 43 anti-bypass scans + fault injection + data flow invariants
- **Security tests**: Sandbox whitelist, env filtering, concurrency locks (~20)
- **Integration tests**: Full run lifecycle, adapters, fallback, OTel (~350)
- **Graph tests**: 61 tests — builder, compilation, invoke, stream, checkpointing, fan-out, routing

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
| Persistence | SQLite (WAL mode) |
| Graph engine | Built-in (LangGraph-compatible API) |
| Distributed tracing | OpenTelemetry (optional) |
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

# OpenTelemetry tracing
pip install -e ".[otel]"

# Everything
pip install -e ".[all]"
```

---

## License

See [LICENSE](LICENSE) for details.

# Agent Framework SDK

Clean public API for embedding AI agents into any Python application.

## Installation

```bash
# Full framework (includes SDK)
pip install agent-framework

# SDK only (lightweight, depends on agent-framework)
pip install agent-framework-sdk

# With specific model provider
pip install agent-framework-sdk[anthropic]
pip install agent-framework-sdk[openai]
pip install agent-framework-sdk[all]
```

## Quick Start

```python
from agent_framework.sdk import AgentSDK, SDKConfig

# Configure
config = SDKConfig(
    model_adapter_type="anthropic",
    api_key="sk-ant-...",
    model_name="claude-sonnet-4-20250514",
)

# Run
async with AgentSDK(config) as sdk:
    result = await sdk.run("Explain this codebase")
    print(result.final_answer)
```

## Streaming

```python
async for event in sdk.run_stream("Build a REST API"):
    if event.type == "token":
        print(event.data["text"], end="", flush=True)
    elif event.type == "tool_start":
        print(f"\n🔧 {event.data['tool_name']}...")
```

## JSONL Output (CI/CD pipelines)

```python
async for line in sdk.run_stream_jsonl("Analyze code"):
    sys.stdout.write(line + "\n")
```

## Custom Tools

```python
@sdk.tool(name="fetch_weather", description="Get current weather")
def fetch_weather(city: str) -> str:
    return f"Weather in {city}: sunny, 22°C"

# Direct tool execution (bypass LLM)
result = await sdk.execute_tool("fetch_weather", {"city": "Tokyo"})
```

## Graph Engine (LangGraph-compatible)

```python
import operator
from typing import Annotated
from typing_extensions import TypedDict

class State(TypedDict):
    messages: Annotated[list[str], operator.add]
    count: int

graph = sdk.create_graph(State)
graph.add_node("greet", lambda s: {"messages": ["Hi!"], "count": s["count"]+1})
graph.add_edge("__start__", "greet")
graph.add_edge("greet", "__end__")

app = sdk.compile_graph(graph)
result = await sdk.invoke_graph(app, {"messages": [], "count": 0})
```

## Parallel Execution

```python
results = await sdk.run_parallel([
    "Analyze auth module",
    "Analyze payment module",
    {"task": "Analyze API layer", "config_overrides": {"max_iterations": 30}},
], max_concurrent=3)
```

## Cancellation

```python
token = sdk.create_cancel_token()
task = asyncio.create_task(sdk.run("long task", cancel_token=token))
# ... later
token.cancel()
```

## Event Callbacks

```python
sdk.on_event("tool_start", lambda data: print(f"🔧 {data}"))
sdk.on_event("error", lambda data: print(f"❌ {data}"))
```

## Health Check

```python
status = sdk.health_check()
print(status)  # {"status": "ready", "model": {...}, "tools": {...}, ...}
```

## Full API Surface

89 public methods + 3 properties covering:

| Category | Methods |
|---|---|
| Execution | `run`, `run_sync`, `run_stream`, `run_stream_jsonl`, `run_isolated`, `run_parallel` |
| Tools | `tool`, `register_tool`, `list_tools`, `execute_tool`, `export_tool_schemas` |
| Graph | `create_graph`, `compile_graph`, `invoke_graph`, `stream_graph`, `create_agent_node` |
| Memory | `list_memories`, `forget_memory`, `pin_memory`, `clear_memories`, +4 more |
| Skills | `register_skill`, `list_skills`, `activate_skill`, `deactivate_skill`, +2 |
| Plugins | `load_plugin`, `enable_plugin`, `disable_plugin`, `list_plugins` |
| MCP | `setup_mcp`, `list_mcp_resources`, `read_mcp_resource`, `get_mcp_prompt` |
| A2A | `setup_a2a`, `build_a2a_server` |
| Conversation | `begin_conversation`, `end_conversation`, `get_history`, `export_history` |
| Checkpoints | `save_checkpoint`, `list_checkpoints`, `restore_checkpoint` |
| Commands | `execute_command` |
| Events | `on_event`, `off_event` |
| Sandbox | `assess_command_risk`, `select_sandbox` |
| IDE | `create_ide_server` |
| Isolation | `create_isolated`, `fork`, `run_isolated`, `run_parallel` |
| Diagnostics | `health_check`, `get_info`, `get_context_stats` |
"""

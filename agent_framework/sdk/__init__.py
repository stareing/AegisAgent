"""Agent Framework SDK — clean public API for third-party embedding.

This package provides a stable, minimal API surface for embedding the
agent framework into external applications (REST APIs, WebSocket servers,
Electron apps, Jupyter notebooks, etc.).

Usage:
    from agent_framework.sdk import AgentSDK, SDKConfig

    sdk = AgentSDK(SDKConfig(model_adapter_type="anthropic", api_key="sk-..."))
    result = await sdk.run("Explain this codebase")
    print(result.final_answer)

    # Streaming
    async for event in sdk.run_stream("Build a REST API"):
        if event.type == "token":
            print(event.data["text"], end="")

    # JSONL streaming (pipe-friendly)
    async for line in sdk.run_stream_jsonl("Analyze code"):
        sys.stdout.write(line + "\\n")

    # Tool registration
    @sdk.tool(name="fetch_weather", description="Get current weather")
    def fetch_weather(city: str) -> str:
        return f"Weather in {city}: sunny, 22°C"

    # Cancellation
    token = sdk.create_cancel_token()
    task = asyncio.create_task(sdk.run("long task", cancel_token=token))
    token.cancel()  # stop the run

    # Event callbacks
    sub_id = sdk.on_event("tool_start", lambda data: print(data))
    sdk.off_event(sub_id)

    # Checkpoints
    cp_id = await sdk.save_checkpoint("before refactor")
    await sdk.restore_checkpoint(cp_id)

    # Fork
    child = sdk.fork({"model_name": "gpt-4"})
    result = await child.run("Solve problem")

    # Async context manager
    async with AgentSDK(config) as sdk:
        result = await sdk.run("Hello")

Design principles:
- SDK imports ONLY from public types — never internal modules
- All configuration through SDKConfig (single entry point)
- Async-first with sync wrappers for convenience
- No global state — each SDK instance is independent
- Full capability coverage — every AgentFramework feature is accessible
"""

from agent_framework.sdk.client import AgentSDK
from agent_framework.sdk.config import SDKConfig
from agent_framework.sdk.types import (
    SDKAgentInfo,
    SDKCancelToken,
    SDKCheckpoint,
    SDKCommandResult,
    SDKContextStats,
    SDKEventSubscription,
    SDKGraphEvent,
    SDKHookInfo,
    SDKIsolatedRunResult,
    SDKMCPServerInfo,
    SDKMemoryEntry,
    SDKModelInfo,
    SDKPluginInfo,
    SDKRunResult,
    SDKSkillInfo,
    SDKStreamEvent,
    SDKStreamEventType,
    SDKTeamNotification,
    SDKToolDefinition,
    SDKToolInfo,
)

__all__ = [
    "AgentSDK",
    "SDKConfig",
    "SDKAgentInfo",
    "SDKCancelToken",
    "SDKCheckpoint",
    "SDKCommandResult",
    "SDKContextStats",
    "SDKEventSubscription",
    "SDKGraphEvent",
    "SDKHookInfo",
    "SDKIsolatedRunResult",
    "SDKMCPServerInfo",
    "SDKMemoryEntry",
    "SDKModelInfo",
    "SDKPluginInfo",
    "SDKRunResult",
    "SDKSkillInfo",
    "SDKStreamEvent",
    "SDKStreamEventType",
    "SDKTeamNotification",
    "SDKToolDefinition",
    "SDKToolInfo",
]

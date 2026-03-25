"""SDK public types — stable API surface for external consumers.

These types are the ONLY data structures that cross the SDK boundary.
Internal framework types (AgentState, SessionState, etc.) are never
exposed to SDK consumers.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, Field


class SDKStreamEventType(str, Enum):
    """Stream event types visible to SDK consumers."""

    TOKEN = "token"
    TOOL_START = "tool_start"
    TOOL_DONE = "tool_done"
    THINKING = "thinking"
    ITERATION_START = "iteration_start"
    SUBAGENT_EVENT = "subagent_event"
    DONE = "done"
    ERROR = "error"


class SDKStreamEvent(BaseModel):
    """A single stream event from SDK run_stream().

    Simplified version of internal StreamEvent — hides framework internals.
    """

    type: SDKStreamEventType
    data: dict[str, Any] = Field(default_factory=dict)
    timestamp_ms: int = 0


class SDKRunResult(BaseModel):
    """Result of an SDK agent run.

    Simplified projection of internal AgentRunResult.
    """

    success: bool = False
    final_answer: str | None = None
    error: str | None = None
    iterations_used: int = 0
    total_tokens: int = 0
    run_id: str = ""
    stop_reason: str = ""
    termination_kind: str = ""
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    progressive_responses: list[str] = Field(default_factory=list)


class SDKToolDefinition(BaseModel):
    """Tool definition for SDK consumers to register custom tools."""

    name: str
    description: str = ""
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    category: str = "custom"
    require_confirm: bool = False
    is_async: bool = False


class SDKToolInfo(BaseModel):
    """Tool information exposed to SDK consumers."""

    name: str = ""
    description: str = ""
    category: str = ""
    source: str = ""
    require_confirm: bool = False
    is_async: bool = False
    tags: list[str] = Field(default_factory=list)


class SDKMemoryEntry(BaseModel):
    """Memory entry visible to SDK consumers."""

    memory_id: str = ""
    content: str = ""
    kind: str = ""
    tags: list[str] = Field(default_factory=list)
    pinned: bool = False
    active: bool = True


class SDKSkillInfo(BaseModel):
    """Skill information visible to SDK consumers."""

    skill_id: str = ""
    name: str = ""
    description: str = ""
    trigger_keywords: list[str] = Field(default_factory=list)
    user_invocable: bool = True
    source_path: str | None = None


class SDKPluginInfo(BaseModel):
    """Plugin information visible to SDK consumers."""

    plugin_id: str = ""
    name: str = ""
    version: str = ""
    description: str = ""
    enabled: bool = False
    state: str = ""


class SDKHookInfo(BaseModel):
    """Hook information visible to SDK consumers."""

    hook_id: str = ""
    hook_point: str = ""
    description: str = ""
    priority: int = 0


class SDKModelInfo(BaseModel):
    """Model catalog entry visible to SDK consumers."""

    model_id: str = ""
    provider: str = ""
    display_name: str = ""
    context_window: int = 0
    supports_vision: bool = False
    supports_tools: bool = False


class SDKAgentInfo(BaseModel):
    """Agent runtime information visible to SDK consumers."""

    agent_id: str = ""
    model_name: str = ""
    adapter_type: str = ""
    approval_mode: str = "DEFAULT"
    max_iterations: int = 20
    shell_enabled: bool = False
    sandbox_enabled: bool = False
    memory_enabled: bool = True
    spawn_enabled: bool = False
    tools_count: int = 0
    skills_count: int = 0
    plugins_count: int = 0
    hooks_count: int = 0
    tools_available: list[str] = Field(default_factory=list)
    skills_available: list[str] = Field(default_factory=list)


class SDKMCPServerInfo(BaseModel):
    """MCP server connection info visible to SDK consumers."""

    server_id: str = ""
    name: str = ""
    connected: bool = False
    tools_count: int = 0


class SDKTeamNotification(BaseModel):
    """Team notification visible to SDK consumers."""

    role: str = ""
    status: str = ""
    summary: str = ""
    task: str = ""
    agent_id: str = ""
    notification_type: str = ""

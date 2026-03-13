from __future__ import annotations

from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


class ToolMeta(BaseModel):
    """Metadata describing a tool.

    Immutability contract:
    - ToolMeta is FROZEN after registration. No module may modify fields
      (especially parameters_schema) at runtime.
    - Visibility can be controlled via ScopedToolRegistry (show/hide tools),
      but the tool's CONTRACT (schema, description, category) is immutable.
    - This prevents race conditions in concurrent runs, A2A sync, and
      MCP schema caching.
    """

    model_config = {"frozen": True}

    name: str
    description: str = ""
    parameters_schema: dict = Field(default_factory=dict)
    category: str = "general"
    require_confirm: bool = False
    is_async: bool = False
    tags: list[str] = Field(default_factory=list)
    source: Literal["local", "mcp", "a2a", "subagent"] = "local"
    namespace: str | None = None
    mcp_server_id: str | None = None
    a2a_agent_url: str | None = None


class ToolEntry(BaseModel):
    """A registered tool entry with callable reference."""

    model_config = {"arbitrary_types_allowed": True}

    meta: ToolMeta
    callable_ref: Callable | None = None
    validator_model: Any = None  # type[BaseModel] | None


class FieldError(BaseModel):
    """A single field validation error."""

    field: str
    expected: str | None = None
    received: str | None = None
    message: str = ""


class ToolExecutionError(BaseModel):
    """Structured tool execution error."""

    error_type: Literal[
        "VALIDATION_ERROR",
        "EXECUTION_ERROR",
        "PERMISSION_DENIED",
        "NOT_FOUND",
        "TIMEOUT",
        "QUOTA_EXCEEDED",
    ]
    error_code: str = ""
    message: str = ""
    field_errors: list[FieldError] | None = None
    retryable: bool = False


class ToolResult(BaseModel):
    """Result of a tool execution.

    output contract:
    - output MUST be JSON-serializable (str, int, float, bool, None, dict, list).
    - Callables, connection objects, raw SDK clients, exception objects are FORBIDDEN.
    - Large outputs must be summarised by the tool/delegation layer before returning.
    - The output is what gets projected into SessionState messages and LLM context.
      It is NOT a raw internal data structure — it is a message-safe projection.
    """

    model_config = {"arbitrary_types_allowed": True}

    tool_call_id: str
    tool_name: str
    success: bool
    output: Any = None
    error: ToolExecutionError | None = None


class ToolExecutionMeta(BaseModel):
    """Metadata about a tool execution."""

    execution_time_ms: int = 0
    source: Literal["local", "mcp", "a2a", "subagent"] = "local"
    trace_ref: str | None = None
    retry_count: int = 0

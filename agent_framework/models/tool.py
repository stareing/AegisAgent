from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Unified Error Code Registry
# ---------------------------------------------------------------------------
# All error codes surfaced to the model or upper layers MUST come from this
# registry. Free-form error strings are FORBIDDEN in error_code fields.
# New codes MUST be appended here — no ad-hoc naming.
#
# Three tiers:
#   GENERAL  — applicable across all subsystems
#   TOOL     — tool validation and execution
#   DELEGATION — subagent and A2A delegation
# ---------------------------------------------------------------------------

class ErrorCode(str, Enum):
    """Canonical error code registry.

    Rule: error_code fields in ToolExecutionError, DelegationSummary,
    IterationError MUST use values from this enum.
    Model-facing error messages MUST reference these codes, not raw
    exception text.
    """

    # General
    TIMEOUT = "TIMEOUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"

    # Tool-specific
    INVALID_ARGUMENT_TYPE = "INVALID_ARGUMENT_TYPE"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    USER_DENIED = "USER_DENIED"
    RUNTIME_ERROR = "RUNTIME_ERROR"

    # Delegation-specific
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    REMOTE_UNAVAILABLE = "REMOTE_UNAVAILABLE"
    DELEGATION_FAILED = "DELEGATION_FAILED"


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


class RetrySafety(BaseModel):
    """Idempotency declaration for auto-retry decisions (v2.6.5 §47).

    Auto-retry is NOT a default-safe behavior. Only operations that are
    explicitly declared retryable AND idempotent (or carry a stable
    idempotency key) may be automatically retried by the framework.

    Rules:
    - retryable=True does NOT imply idempotent=True
    - Auto-retry requires: retryable=True AND (idempotent=True OR idempotency_key is set)
    - Read-only tools may declare idempotent=True
    - External write tools default to idempotent=False
    - Without idempotency guarantee, RETRY means "allow upper layer to re-plan",
      NOT "automatically replay the side-effecting operation"
    """

    retryable: bool = False
    idempotent: bool = False
    idempotency_key: str | None = None
    max_retry_attempts: int = 3
    retry_scope: str = "tool"  # "model" | "tool" | "delegation" | "infra"


class RetryDecision(BaseModel):
    """Structured decision on whether to retry an operation (v2.6.5 §47).

    Produced by the retry decision logic. Must be based on RetrySafety,
    not just error type.
    """

    should_retry: bool = False
    reason: str = ""
    retry_safety: RetrySafety = Field(default_factory=RetrySafety)
    attempt_index: int = 0


class ToolExecutionOutcome(BaseModel):
    """Structured outcome of a single tool execution (v2.6.4 §43).

    Captures both the result and any side-effect references produced during
    execution. The ToolCommitSequencer uses input_index to ensure stable
    commit ordering regardless of execution completion order.

    Side-effect visibility rules:
    - Tool execution threads MUST NOT directly write SessionState
    - Tool execution threads MUST NOT directly register artifacts
    - Tool execution threads MUST NOT directly write audit records
    - All side effects are collected here and committed via ToolCommitSequencer
    """

    tool_call_id: str = ""
    input_index: int = 0
    result: ToolResult | None = None
    execution_meta: ToolExecutionMeta = Field(default_factory=ToolExecutionMeta)
    artifact_refs: list[Any] = Field(default_factory=list)
    side_effect_refs: list[str] = Field(default_factory=list)


class AuthorizationDecision(BaseModel):
    """Structured result of the tool authorization chain.

    Every layer in the authorization chain (CapabilityPolicy, ScopedToolRegistry,
    on_tool_call_requested) MUST produce one of these — not a bare bool.
    This enables auditing and debugging of permission denials.

    Authorization chain priority:
    1. CapabilityPolicy — capability ceiling (HARD)
    2. ScopedToolRegistry — visibility set (NOT security boundary)
    3. on_tool_call_requested() — runtime agent hook (final gate)
    Any layer rejecting → overall rejection.
    """

    allowed: bool
    reason: str = ""
    source_layer: str = ""
    normalized_tool_name: str = ""
    matched_policy_id: str | None = None

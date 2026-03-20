"""Hook data models — stable DTOs for the hooks subsystem.

All hook-related pydantic models live here (under models/).
The hooks/ package re-exports from this module.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Hook Point — framework-fixed extension points
# ---------------------------------------------------------------------------

class HookPoint(str, Enum):
    """Framework-predefined hook execution points.

    New hook points MUST be added here — plugins cannot invent their own.
    """

    RUN_START = "run.start"
    RUN_FINISH = "run.finish"
    RUN_ERROR = "run.error"

    ITERATION_START = "iteration.start"
    ITERATION_FINISH = "iteration.finish"
    ITERATION_ERROR = "iteration.error"

    PRE_TOOL_USE = "tool.pre_use"
    POST_TOOL_USE = "tool.post_use"
    TOOL_ERROR = "tool.error"

    PRE_DELEGATION = "delegation.pre"
    POST_DELEGATION = "delegation.post"
    DELEGATION_ERROR = "delegation.error"

    MEMORY_PRE_RECORD = "memory.pre_record"
    MEMORY_POST_RECORD = "memory.post_record"

    CONTEXT_PRE_BUILD = "context.pre_build"
    CONTEXT_POST_BUILD = "context.post_build"

    ARTIFACT_PRODUCED = "artifact.produced"
    ARTIFACT_FINALIZE = "artifact.finalize"

    CONFIG_LOADED = "config.loaded"
    INSTRUCTIONS_LOADED = "instructions.loaded"


# Hook points that allow DENY action
DENIABLE_HOOK_POINTS: frozenset[HookPoint] = frozenset({
    HookPoint.PRE_TOOL_USE,
    HookPoint.PRE_DELEGATION,
    HookPoint.MEMORY_PRE_RECORD,
    HookPoint.CONTEXT_PRE_BUILD,
})


# ---------------------------------------------------------------------------
# Hook classification and behavior enums
# ---------------------------------------------------------------------------

class HookCategory(str, Enum):
    """Three categories with distinct execution semantics."""

    COMMAND = "command"    # Deterministic, sync-safe, gate/audit
    PROMPT = "prompt"     # Advisory, LLM-assisted suggestions
    AGENT = "agent"       # Complex logic via controlled sub-agent


class HookExecutionMode(str, Enum):
    SYNC = "sync"
    ASYNC = "async"


class HookFailurePolicy(str, Enum):
    """What happens when a hook fails."""

    IGNORE = "ignore"
    WARN = "warn"
    FAIL_CLOSED = "fail_closed"


class HookResultAction(str, Enum):
    """Actions a hook can request."""

    NOOP = "noop"
    ALLOW = "allow"
    DENY = "deny"
    MODIFY = "modify"
    EMIT_ARTIFACT = "emit_artifact"
    REQUEST_CONFIRMATION = "request_confirmation"


# ---------------------------------------------------------------------------
# Hook metadata — describes a registered hook
# ---------------------------------------------------------------------------

class HookMeta(BaseModel):
    """Immutable descriptor of a registered hook."""

    model_config = {"frozen": True}

    hook_id: str
    plugin_id: str = "builtin"
    name: str = ""
    hook_point: HookPoint
    category: HookCategory = HookCategory.COMMAND
    description: str = ""
    execution_mode: HookExecutionMode = HookExecutionMode.SYNC
    failure_policy: HookFailurePolicy = HookFailurePolicy.WARN
    priority: int = 100
    timeout_ms: int = 3000
    enabled: bool = True
    read_only: bool = True
    tags: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Hook execution context — DTO snapshot passed to hooks
# ---------------------------------------------------------------------------

class HookContext(BaseModel):
    """Stable DTO passed to hooks at execution time.

    Hooks MUST NOT receive mutable state objects (SessionState,
    AgentState, RunStateController, etc.). Only DTO snapshots.
    The payload dict is deep-copied on construction so hooks
    cannot mutate the caller's data.
    """

    model_config = {"frozen": True}

    run_id: str | None = None
    iteration_id: str | None = None
    attempt_id: str | None = None
    agent_id: str | None = None
    parent_run_id: str | None = None
    user_id: str | None = None
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _freeze_payload(cls, data: Any) -> Any:
        if isinstance(data, dict) and "payload" in data:
            data["payload"] = copy.deepcopy(data["payload"])
        return data


# ---------------------------------------------------------------------------
# Hook result — structured response from hook execution
# ---------------------------------------------------------------------------

class HookResult(BaseModel):
    """Structured result from hook execution.

    Rules:
    - DENY is only valid for DENIABLE_HOOK_POINTS
    - MODIFY can only change whitelisted fields per hook point
    - emitted_artifacts go through ArtifactManager (not direct registration)
    - audit_data is for external audit systems, not core state
    """

    hook_id: str = ""
    action: HookResultAction = HookResultAction.NOOP
    message: str | None = None
    modified_payload: dict[str, Any] | None = None
    emitted_artifacts: list[dict[str, Any]] = Field(default_factory=list)
    audit_data: dict[str, Any] | None = None
    error_code: str | None = None

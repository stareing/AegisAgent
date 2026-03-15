"""Stream event model for real-time agent output.

StreamEvent is the sole event type yielded by run_stream(). The integration
layer (TUI, API, WebSocket) consumes these events for incremental rendering.

Boundary (extends v2.6.1 §34):
- StreamEvents are transient UI events — they MUST NOT enter SessionState.
- Only the final AgentRunResult (carried by the DONE event) is authoritative.
- If streaming is interrupted, the consumer should treat partial output as
  provisional and discard it.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class StreamEventType(str, Enum):
    """Types of stream events emitted during a run."""

    TOKEN = "token"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DONE = "tool_call_done"
    ITERATION_START = "iteration_start"
    DONE = "done"
    ERROR = "error"
    # Progressive sub-agent events (transient UI — never enter SessionState)
    SUBAGENT_START = "subagent_start"
    SUBAGENT_DONE = "subagent_done"
    PROGRESSIVE_RESPONSE = "progressive_response"


class StreamEvent(BaseModel):
    """A single stream event from run_stream().

    Attributes:
        type: Event classification.
        data: Payload — structure depends on type:
            TOKEN:                {"text": str}
            TOOL_CALL_START:      {"tool_name": str, "tool_call_id": str, "arguments": dict}
            TOOL_CALL_DONE:       {"tool_name": str, "tool_call_id": str, "success": bool, "output": str}
            ITERATION_START:      {"iteration_index": int}
            DONE:                 {"result": AgentRunResult}
            ERROR:                {"error": str, "error_type": str}
            SUBAGENT_START:       {"tool_call_id": str, "task_input": str, "index": int, "total": int}
            SUBAGENT_DONE:        {"tool_call_id": str, "task_input": str, "success": bool, "output": str, "index": int, "total": int}
            PROGRESSIVE_RESPONSE: {"text": str, "index": int, "total": int}
    """

    type: StreamEventType
    data: dict[str, Any] = Field(default_factory=dict)

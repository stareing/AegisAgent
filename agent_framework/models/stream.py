"""Stream event model for real-time agent output.

StreamEvent is the sole event type yielded by run_stream(). The integration
layer (TUI, API, WebSocket) consumes these events for incremental rendering.

Boundary (extends v2.6.1 §34):
- StreamEvents are transient UI events — they MUST NOT enter SessionState.
- Only the final AgentRunResult (carried by the DONE event) is authoritative.
- If streaming is interrupted, the consumer should treat partial output as
  provisional and discard it.

JSONL output mode (Gemini-inspired):
- StreamEvent.to_jsonl() serializes to a single-line JSON string (JSONL format).
- JSONLStreamWriter wraps an async generator to write JSONL to any file-like object.
- Suitable for CI/CD pipelines, log aggregation, and inter-process communication.
"""

from __future__ import annotations

import json
import time
from enum import Enum
from typing import IO, Any, AsyncGenerator

from pydantic import BaseModel, Field


class StreamEventType(str, Enum):
    """Types of stream events emitted during a run."""

    TOKEN = "token"
    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_DONE = "tool_call_done"
    ITERATION_START = "iteration_start"
    DONE = "done"
    ERROR = "error"
    # Thinking / reasoning block events (provider think-tag parsing)
    THINKING_START = "thinking_start"
    THINKING_DELTA = "thinking_delta"
    THINKING_END = "thinking_end"
    ASSISTANT_TOOL_CALLS = "assistant_tool_calls"
    # Progressive tool-completion events (transient UI — never enter SessionState)
    # Fired for every tool in progressive mode, not just spawn_agent.
    PROGRESSIVE_START = "progressive_start"
    PROGRESSIVE_DONE = "progressive_done"
    PROGRESSIVE_RESPONSE = "progressive_response"
    # Real-time sub-agent stream event — forwarded child TOKEN/TOOL/ITERATION
    # data: {"spawn_id": str, "label": str, "event_type": str, ...inner data}
    SUBAGENT_STREAM = "subagent_stream"
    # Backward-compatible aliases — existing consumers that check SUBAGENT_* still work
    SUBAGENT_START = "progressive_start"
    SUBAGENT_DONE = "progressive_done"
    # Background progress summary (v4.0) — emitted by ProgressSummarizer
    PROGRESS_SUMMARY = "progress_summary"


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
            ASSISTANT_TOOL_CALLS: {"content": str | None, "tool_calls": list[ToolCallRequest]}
            PROGRESSIVE_START:    {"tool_call_id": str, "tool_name": str, "description": str, "index": int, "total": int}
            PROGRESSIVE_DONE:     {"tool_call_id": str, "tool_name": str, "description": str, "success": bool, "output": str, "index": int, "total": int}
            PROGRESSIVE_RESPONSE: {"text": str, "index": int, "total": int}
            SUBAGENT_STREAM:     {"spawn_id": str, "label": str, "event_type": str, ...inner_data}
    """

    type: StreamEventType
    data: dict[str, Any] = Field(default_factory=dict)

    def to_jsonl(self) -> str:
        """Serialize to a single-line JSON string (JSONL format).

        The output is a compact JSON object with "type", "data", and
        "timestamp_ms" fields. Non-serializable values in data are
        coerced to strings.

        Returns:
            A single line of JSON (no trailing newline).
        """
        payload: dict[str, Any] = {
            "type": self.type.value,
            "timestamp_ms": int(time.time() * 1000),
        }

        # Sanitize data — AgentRunResult and ToolCallRequest are not
        # directly JSON-serializable, so we convert via pydantic
        sanitized_data: dict[str, Any] = {}
        for key, value in self.data.items():
            if hasattr(value, "model_dump"):
                sanitized_data[key] = value.model_dump(mode="json")
            elif isinstance(value, list):
                sanitized_data[key] = [
                    v.model_dump(mode="json") if hasattr(v, "model_dump") else v
                    for v in value
                ]
            else:
                sanitized_data[key] = value

        payload["data"] = sanitized_data

        return json.dumps(payload, default=str, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> StreamEvent:
        """Deserialize from a JSONL line.

        Args:
            line: A single JSON line (with or without trailing newline).

        Returns:
            StreamEvent instance.
        """
        obj = json.loads(line.strip())
        return cls(
            type=StreamEventType(obj["type"]),
            data=obj.get("data", {}),
        )


class JSONLStreamWriter:
    """Writes StreamEvents as JSONL lines to a file-like object.

    Suitable for piping to stdout, log files, or named pipes.

    Usage:
        import sys
        writer = JSONLStreamWriter(sys.stdout)

        async for event in framework.run_stream("task"):
            writer.write(event)

    Or as an async consumer:
        async for event in framework.run_stream("task"):
            await writer.write_async(event)
    """

    def __init__(self, output: IO[str] | None = None) -> None:
        """Initialize with a file-like object. Defaults to sys.stdout."""
        if output is None:
            import sys
            output = sys.stdout
        self._output = output
        self._event_count = 0

    def write(self, event: StreamEvent) -> None:
        """Write a single event as a JSONL line (synchronous)."""
        line = event.to_jsonl()
        self._output.write(line + "\n")
        self._output.flush()
        self._event_count += 1

    async def write_async(self, event: StreamEvent) -> None:
        """Write a single event (async-compatible wrapper)."""
        self.write(event)

    async def consume_stream(
        self, stream: AsyncGenerator[StreamEvent, None]
    ) -> None:
        """Consume an entire stream, writing each event as JSONL."""
        async for event in stream:
            self.write(event)

    @property
    def event_count(self) -> int:
        """Number of events written so far."""
        return self._event_count

    def close(self) -> None:
        """Flush and close the output (if closeable)."""
        try:
            self._output.flush()
        except Exception:
            pass

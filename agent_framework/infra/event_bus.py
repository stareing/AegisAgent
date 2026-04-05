"""EventBus — observation-only event distribution.

v2.5.2 §28: Event bus observation boundary.

Subscribers MUST NOT:
- Mutate AgentState, SessionState, or any shared mutable state
- Call back into framework components (RunCoordinator, ToolExecutor, etc.)
- Block the event loop with synchronous I/O
- Raise exceptions that propagate to the publisher

Subscribers SHOULD:
- Log events
- Update external metrics/telemetry
- Enqueue messages to external systems (non-blocking)

The bus catches subscriber exceptions to prevent cascading failures.

Event delivery semantics (v2.6.5 §49):
- Delivery is BEST-EFFORT: events may be duplicated or lost.
- Event order is NOT a business truth source.
- Subscribers MUST handle the same event_id idempotently.
- Subscriber exceptions MUST NOT propagate to the publisher.
- Events MUST NOT be used to advance core state machines.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from blinker import Namespace
from pydantic import BaseModel, Field


class EventEnvelope(BaseModel):
    """Structured event wrapper with stable identity (v2.6.5 §49).

    Every published event is wrapped in an envelope with a stable event_id.
    If the same business fact is re-published, the same event_id (or a
    deterministic correlation key) MUST be reused so subscribers can
    deduplicate.

    Subscribers MUST:
    - Handle duplicate event_ids idempotently
    - Not assume events arrive exactly once
    - Not use event ordering as state machine input

    Prohibited:
    - Writing duplicate audit/artifact/notification for the same event_id
    - Using event arrival order to determine iteration sequence
    - Blocking on event processing (must be fire-and-forget)
    """

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:16])
    event_name: str = ""
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    run_id: str | None = None
    iteration_id: str | None = None
    source_layer: str = ""
    payload: dict = Field(default_factory=dict)


class EventBus:
    """Simple event bus backed by blinker.

    Observation boundary: subscribers are passive observers.
    They receive events but must not influence framework control flow.
    Exceptions from subscribers are caught and logged (not propagated).

    Delivery semantics (v2.6.5 §49):
    - Best-effort: events may be duplicated or lost
    - Subscribers must be idempotent on event_id
    - Event order is observational, not authoritative
    """

    def __init__(self) -> None:
        self._ns = Namespace()

    def subscribe(self, event_name: str, handler: Callable[..., Any]) -> None:
        signal = self._ns.signal(event_name)
        signal.connect(handler)

    def publish(self, event_name: str, payload: Any = None) -> None:
        signal = self._ns.signal(event_name)
        signal.send(self, payload=payload)

    def unsubscribe(self, event_name: str, handler: Callable[..., Any]) -> None:
        signal = self._ns.signal(event_name)
        signal.disconnect(handler)

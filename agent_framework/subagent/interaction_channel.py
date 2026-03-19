"""InMemoryInteractionChannel — structured event channel for parent-child interaction.

Implements SubAgentInteractionChannelProtocol with in-memory storage.
Events are append-only per spawn_id, sequence_no is strictly monotonic.

Thread-safety: uses threading.Lock for concurrent access (coordinator +
sub-agent runtime may operate from different coroutines/threads).
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from agent_framework.models.subagent import (AckLevel, DelegationEvent,
                                             DelegationEventType)


class InMemoryInteractionChannel:
    """In-memory event channel for parent-child long-term interaction.

    Per-spawn_id event streams with monotonic sequence numbers.
    Append-only — events cannot be modified or deleted.
    """

    def __init__(self, max_events_per_spawn: int = 200) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, list[DelegationEvent]] = defaultdict(list)
        self._seq_counters: dict[str, int] = defaultdict(int)
        self._max_events_per_spawn = max_events_per_spawn

    def append_event(self, event: DelegationEvent) -> None:
        """Append an event to the spawn_id's stream.

        Assigns event_id and sequence_no if not set.
        Raises ValueError if max events exceeded.
        """
        with self._lock:
            spawn_id = event.spawn_id
            stream = self._events[spawn_id]

            if len(stream) >= self._max_events_per_spawn:
                raise ValueError(
                    f"Max events ({self._max_events_per_spawn}) exceeded for spawn {spawn_id}"
                )

            # Auto-assign event_id if empty
            if not event.event_id:
                event = event.model_copy(
                    update={"event_id": f"evt_{uuid.uuid4().hex[:12]}"}
                )

            # Assign monotonic sequence_no
            self._seq_counters[spawn_id] += 1
            seq = self._seq_counters[spawn_id]
            event = event.model_copy(update={"sequence_no": seq})

            # Ensure created_at is set
            if event.created_at is None:
                event = event.model_copy(
                    update={"created_at": datetime.now(timezone.utc)}
                )

            stream.append(event)

    def list_events(
        self,
        spawn_id: str,
        after_sequence_no: int | None = None,
    ) -> list[DelegationEvent]:
        """List events for a spawn_id, optionally after a given sequence number."""
        with self._lock:
            stream = self._events.get(spawn_id, [])
            if after_sequence_no is not None:
                return [e for e in stream if e.sequence_no > after_sequence_no]
            return list(stream)

    def ack_event(
        self, spawn_id: str, event_id: str,
        level: AckLevel = AckLevel.RECEIVED,
    ) -> None:
        """Acknowledge an event at the specified level.

        Boundary §4: ack levels are monotonic (NONE < RECEIVED < PROJECTED < HANDLED).
        Can only advance the ack level, never regress.
        """
        with self._lock:
            stream = self._events.get(spawn_id, [])
            for i, event in enumerate(stream):
                if event.event_id == event_id:
                    # Only advance ack level, never regress
                    current = list(AckLevel).index(event.ack_level)
                    target = list(AckLevel).index(level)
                    if target > current:
                        stream[i] = event.model_copy(update={"ack_level": level})
                    return
            raise ValueError(f"Event {event_id} not found for spawn {spawn_id}")

    def get_latest_sequence_no(self, spawn_id: str) -> int:
        """Return the latest sequence number for a spawn_id (0 if no events)."""
        with self._lock:
            return self._seq_counters.get(spawn_id, 0)

    def get_pending_events(self, spawn_id: str) -> list[DelegationEvent]:
        """Return events that require_ack but haven't been acked (ack_level=NONE)."""
        with self._lock:
            stream = self._events.get(spawn_id, [])
            return [
                e for e in stream
                if e.requires_ack and e.ack_level == AckLevel.NONE
            ]

    def get_unacked_questions(self, spawn_id: str) -> list[DelegationEvent]:
        """Return QUESTION/CONFIRMATION_REQUEST events not yet HANDLED."""
        with self._lock:
            stream = self._events.get(spawn_id, [])
            return [
                e for e in stream
                if e.event_type in (
                    DelegationEventType.QUESTION,
                    DelegationEventType.CONFIRMATION_REQUEST,
                )
                and e.ack_level != AckLevel.HANDLED
            ]

    def drain_new_events(
        self, spawn_id: str, last_seen_seq: int
    ) -> list[DelegationEvent]:
        """Drain events newer than last_seen_seq. Non-blocking."""
        return self.list_events(spawn_id, after_sequence_no=last_seen_seq)

    def clear_spawn(self, spawn_id: str) -> None:
        """Remove all events for a spawn_id. Only for cleanup after completion."""
        with self._lock:
            self._events.pop(spawn_id, None)
            self._seq_counters.pop(spawn_id, None)

    @property
    def active_spawn_ids(self) -> list[str]:
        """Return spawn_ids that have events."""
        with self._lock:
            return [sid for sid, events in self._events.items() if events]

    def emit_event(
        self,
        spawn_id: str,
        parent_run_id: str,
        event_type: DelegationEventType,
        payload: dict | None = None,
        requires_ack: bool = False,
    ) -> DelegationEvent:
        """Convenience: build and append an event in one call."""
        event = DelegationEvent(
            spawn_id=spawn_id,
            parent_run_id=parent_run_id,
            event_type=event_type,
            payload=payload or {},
            requires_ack=requires_ack,
        )
        self.append_event(event)
        # Return the event with assigned event_id and sequence_no
        return self._events[spawn_id][-1]

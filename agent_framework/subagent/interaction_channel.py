"""Interaction channels — structured event channel for parent-child interaction.

Two implementations:
- InMemoryInteractionChannel: fast, volatile (original)
- SQLiteInteractionChannel: persistent, crash-recoverable

Both implement SubAgentInteractionChannelProtocol.
Events are append-only per spawn_id, sequence_no is strictly monotonic.

Thread-safety: uses threading.Lock for concurrent access (coordinator +
sub-agent runtime may operate from different coroutines/threads).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

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


# ---------------------------------------------------------------------------
# SQLite-backed persistent channel
# ---------------------------------------------------------------------------

_CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS delegation_events (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    spawn_id TEXT NOT NULL,
    parent_run_id TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sequence_no INTEGER NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    requires_ack INTEGER NOT NULL DEFAULT 0,
    ack_level TEXT NOT NULL DEFAULT 'NONE'
);
"""

_CREATE_EVENTS_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_evt_spawn ON delegation_events (spawn_id, sequence_no);",
    "CREATE INDEX IF NOT EXISTS idx_evt_ack ON delegation_events (spawn_id, ack_level);",
]


class SQLiteInteractionChannel:
    """SQLite-backed persistent event channel for parent-child interaction.

    Same API as InMemoryInteractionChannel but survives process restarts.
    Pending HITL requests can be recovered after crash via get_pending_events().
    """

    def __init__(
        self,
        db_path: str = "data/interaction_events.db",
        max_events_per_spawn: int = 200,
    ) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._max_events_per_spawn = max_events_per_spawn
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_EVENTS_TABLE)
            for idx_sql in _CREATE_EVENTS_INDEXES:
                self._conn.execute(idx_sql)

    # -- helpers --

    def _row_to_event(self, row: sqlite3.Row) -> DelegationEvent:
        return DelegationEvent(
            event_id=row["event_id"],
            spawn_id=row["spawn_id"],
            parent_run_id=row["parent_run_id"],
            event_type=DelegationEventType(row["event_type"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            sequence_no=row["sequence_no"],
            payload=json.loads(row["payload"]),
            requires_ack=bool(row["requires_ack"]),
            ack_level=AckLevel(row["ack_level"]),
        )

    def _count_events(self, spawn_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM delegation_events WHERE spawn_id = ?",
            (spawn_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def _next_seq(self, spawn_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(sequence_no) AS mx FROM delegation_events WHERE spawn_id = ?",
            (spawn_id,),
        ).fetchone()
        current = row["mx"] if row and row["mx"] is not None else 0
        return current + 1

    # -- public API (matches InMemoryInteractionChannel) --

    def append_event(self, event: DelegationEvent) -> None:
        """Append an event to the spawn_id's stream, persisted to SQLite."""
        with self._lock:
            spawn_id = event.spawn_id
            if self._count_events(spawn_id) >= self._max_events_per_spawn:
                raise ValueError(
                    f"Max events ({self._max_events_per_spawn}) exceeded for spawn {spawn_id}"
                )

            event_id = event.event_id or f"evt_{uuid.uuid4().hex[:12]}"
            seq = self._next_seq(spawn_id)
            created_at = (
                event.created_at or datetime.now(timezone.utc)
            ).isoformat()
            payload_json = json.dumps(event.payload, default=str)

            with self._conn:
                self._conn.execute(
                    """INSERT INTO delegation_events
                       (event_id, spawn_id, parent_run_id, event_type,
                        created_at, sequence_no, payload, requires_ack, ack_level)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        event_id, spawn_id, event.parent_run_id,
                        event.event_type.value, created_at, seq,
                        payload_json, int(event.requires_ack),
                        event.ack_level.value,
                    ),
                )

    def list_events(
        self,
        spawn_id: str,
        after_sequence_no: int | None = None,
    ) -> list[DelegationEvent]:
        """List events for a spawn_id, optionally after a given sequence number."""
        with self._lock:
            if after_sequence_no is not None:
                rows = self._conn.execute(
                    "SELECT * FROM delegation_events WHERE spawn_id = ? AND sequence_no > ? ORDER BY sequence_no",
                    (spawn_id, after_sequence_no),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM delegation_events WHERE spawn_id = ? ORDER BY sequence_no",
                    (spawn_id,),
                ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def ack_event(
        self, spawn_id: str, event_id: str,
        level: AckLevel = AckLevel.RECEIVED,
    ) -> None:
        """Acknowledge an event. Monotonic: only advances, never regresses."""
        ack_order = list(AckLevel)
        with self._lock:
            row = self._conn.execute(
                "SELECT ack_level FROM delegation_events WHERE spawn_id = ? AND event_id = ?",
                (spawn_id, event_id),
            ).fetchone()
            if row is None:
                raise ValueError(f"Event {event_id} not found for spawn {spawn_id}")

            current_idx = ack_order.index(AckLevel(row["ack_level"]))
            target_idx = ack_order.index(level)
            if target_idx > current_idx:
                with self._conn:
                    self._conn.execute(
                        "UPDATE delegation_events SET ack_level = ? WHERE event_id = ?",
                        (level.value, event_id),
                    )

    def get_latest_sequence_no(self, spawn_id: str) -> int:
        """Return the latest sequence number for a spawn_id (0 if no events)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(sequence_no) AS mx FROM delegation_events WHERE spawn_id = ?",
                (spawn_id,),
            ).fetchone()
            return row["mx"] if row and row["mx"] is not None else 0

    def get_pending_events(self, spawn_id: str) -> list[DelegationEvent]:
        """Return events that require_ack but haven't been acked (ack_level=NONE)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM delegation_events WHERE spawn_id = ? AND requires_ack = 1 AND ack_level = 'NONE' ORDER BY sequence_no",
                (spawn_id,),
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def get_unacked_questions(self, spawn_id: str) -> list[DelegationEvent]:
        """Return QUESTION/CONFIRMATION_REQUEST events not yet HANDLED."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM delegation_events WHERE spawn_id = ? AND event_type IN ('QUESTION', 'CONFIRMATION_REQUEST') AND ack_level != 'HANDLED' ORDER BY sequence_no",
                (spawn_id,),
            ).fetchall()
            return [self._row_to_event(r) for r in rows]

    def drain_new_events(
        self, spawn_id: str, last_seen_seq: int,
    ) -> list[DelegationEvent]:
        """Drain events newer than last_seen_seq. Non-blocking."""
        return self.list_events(spawn_id, after_sequence_no=last_seen_seq)

    def clear_spawn(self, spawn_id: str) -> None:
        """Remove all events for a spawn_id. Only for cleanup after completion."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "DELETE FROM delegation_events WHERE spawn_id = ?",
                    (spawn_id,),
                )

    @property
    def active_spawn_ids(self) -> list[str]:
        """Return spawn_ids that have events."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT spawn_id FROM delegation_events"
            ).fetchall()
            return [r["spawn_id"] for r in rows]

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
        # Return the event with assigned fields from DB
        events = self.list_events(spawn_id)
        return events[-1]

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()

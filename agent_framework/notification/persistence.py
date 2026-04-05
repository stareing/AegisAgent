"""Bus persistence backends — InMemory and SQLite.

Implements BusPersistence protocol for durable message storage.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from agent_framework.models.subagent import AckLevel
from agent_framework.notification.envelope import BusAddress, BusEnvelope


class BusPersistence(Protocol):
    """Persistence backend protocol for AgentBus."""

    def store(self, envelope: BusEnvelope) -> None: ...
    def load_pending(self, agent_id: str, group: str = "") -> list[BusEnvelope]: ...
    def mark_delivered(self, envelope_id: str) -> None: ...
    def mark_acked(self, envelope_id: str, level: AckLevel) -> None: ...
    def get_envelope(self, envelope_id: str) -> BusEnvelope | None: ...
    def cleanup_expired(self) -> int: ...
    def cleanup_group(self, group: str) -> int: ...
    def close(self) -> None: ...


class InMemoryBusPersistence:
    """In-memory persistence backend. Fast but volatile."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._envelopes: dict[str, BusEnvelope] = {}
        self._delivered: set[str] = set()

    def store(self, envelope: BusEnvelope) -> None:
        with self._lock:
            self._envelopes[envelope.envelope_id] = envelope

    def load_pending(self, agent_id: str, group: str = "") -> list[BusEnvelope]:
        with self._lock:
            result = []
            for env in self._envelopes.values():
                if env.envelope_id in self._delivered:
                    continue
                # Match by target agent_id or group
                if env.target and env.target.agent_id == agent_id:
                    result.append(env)
                elif not env.target and group and env.source.group == group:
                    # Broadcast within group — exclude sender
                    if env.source.agent_id != agent_id:
                        result.append(env)
            return sorted(result, key=lambda e: (e.priority, e.created_at))

    def mark_delivered(self, envelope_id: str) -> None:
        with self._lock:
            self._delivered.add(envelope_id)

    def mark_acked(self, envelope_id: str, level: AckLevel) -> None:
        with self._lock:
            env = self._envelopes.get(envelope_id)
            if env is None:
                return
            ack_order = list(AckLevel)
            current = ack_order.index(env.ack_level)
            target = ack_order.index(level)
            if target > current:
                self._envelopes[envelope_id] = env.model_copy(update={"ack_level": level})

    def get_envelope(self, envelope_id: str) -> BusEnvelope | None:
        with self._lock:
            env = self._envelopes.get(envelope_id)
            return env.model_copy() if env else None

    def cleanup_expired(self) -> int:
        now = datetime.now(timezone.utc)
        with self._lock:
            expired = [
                eid for eid, env in self._envelopes.items()
                if env.ttl_ms > 0
                and (now - env.created_at).total_seconds() * 1000 > env.ttl_ms
            ]
            for eid in expired:
                self._envelopes.pop(eid, None)
                self._delivered.discard(eid)
            return len(expired)

    def cleanup_group(self, group: str) -> int:
        with self._lock:
            to_remove = [
                eid for eid, env in self._envelopes.items()
                if env.source.group == group
                or (env.target and env.target.group == group)
            ]
            for eid in to_remove:
                self._envelopes.pop(eid, None)
                self._delivered.discard(eid)
            return len(to_remove)

    def close(self) -> None:
        pass


class SQLiteBusPersistence:
    """SQLite-backed persistence. Survives process restarts."""

    _CREATE_SQL = """
    CREATE TABLE IF NOT EXISTS bus_envelopes (
        envelope_id TEXT PRIMARY KEY,
        topic TEXT NOT NULL,
        source_agent_id TEXT NOT NULL,
        source_group TEXT NOT NULL DEFAULT '',
        target_agent_id TEXT,
        target_group TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        correlation_id TEXT NOT NULL DEFAULT '',
        reply_to TEXT NOT NULL DEFAULT '',
        ttl_ms INTEGER NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL DEFAULT 5,
        requires_ack INTEGER NOT NULL DEFAULT 0,
        ack_level TEXT NOT NULL DEFAULT 'NONE',
        delivered INTEGER NOT NULL DEFAULT 0
    );
    """
    _INDEXES = [
        "CREATE INDEX IF NOT EXISTS idx_bus_target ON bus_envelopes (target_agent_id, delivered);",
        "CREATE INDEX IF NOT EXISTS idx_bus_group ON bus_envelopes (target_group, delivered);",
        "CREATE INDEX IF NOT EXISTS idx_bus_corr ON bus_envelopes (correlation_id);",
    ]

    def __init__(self, db_path: str = "data/agent_bus.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._conn:
            self._conn.execute(self._CREATE_SQL)
            for idx in self._INDEXES:
                self._conn.execute(idx)

    def store(self, envelope: BusEnvelope) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR REPLACE INTO bus_envelopes
                   (envelope_id, topic, source_agent_id, source_group,
                    target_agent_id, target_group, payload_json, created_at,
                    correlation_id, reply_to, ttl_ms, priority,
                    requires_ack, ack_level, delivered)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
                (
                    envelope.envelope_id, envelope.topic,
                    envelope.source.agent_id, envelope.source.group,
                    envelope.target.agent_id if envelope.target else None,
                    envelope.target.group if envelope.target else "",
                    json.dumps(envelope.payload, default=str),
                    envelope.created_at.isoformat(),
                    envelope.correlation_id, envelope.reply_to,
                    envelope.ttl_ms, envelope.priority,
                    int(envelope.requires_ack), envelope.ack_level.value,
                ),
            )

    def load_pending(self, agent_id: str, group: str = "") -> list[BusEnvelope]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM bus_envelopes
                   WHERE delivered = 0
                   AND (target_agent_id = ? OR (target_agent_id IS NULL AND source_group = ? AND source_agent_id != ?))
                   ORDER BY priority, created_at""",
                (agent_id, group, agent_id),
            ).fetchall()
            return [self._row_to_envelope(r) for r in rows]

    def mark_delivered(self, envelope_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE bus_envelopes SET delivered = 1 WHERE envelope_id = ?",
                (envelope_id,),
            )

    def mark_acked(self, envelope_id: str, level: AckLevel) -> None:
        ack_order = list(AckLevel)
        with self._lock:
            row = self._conn.execute(
                "SELECT ack_level FROM bus_envelopes WHERE envelope_id = ?",
                (envelope_id,),
            ).fetchone()
            if row is None:
                return
            current = ack_order.index(AckLevel(row["ack_level"]))
            target = ack_order.index(level)
            if target > current:
                with self._conn:
                    self._conn.execute(
                        "UPDATE bus_envelopes SET ack_level = ? WHERE envelope_id = ?",
                        (level.value, envelope_id),
                    )

    def get_envelope(self, envelope_id: str) -> BusEnvelope | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bus_envelopes WHERE envelope_id = ?",
                (envelope_id,),
            ).fetchone()
            return self._row_to_envelope(row) if row else None

    def cleanup_expired(self) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """DELETE FROM bus_envelopes
                   WHERE ttl_ms > 0
                   AND julianday(?) - julianday(created_at) > ttl_ms / 86400000.0""",
                (now,),
            )
            return cursor.rowcount

    def cleanup_group(self, group: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "DELETE FROM bus_envelopes WHERE source_group = ? OR target_group = ?",
                (group, group),
            )
            return cursor.rowcount

    def close(self) -> None:
        self._conn.close()

    def _row_to_envelope(self, row: sqlite3.Row) -> BusEnvelope:
        target = None
        if row["target_agent_id"]:
            target = BusAddress(
                agent_id=row["target_agent_id"],
                group=row["target_group"],
            )
        return BusEnvelope(
            envelope_id=row["envelope_id"],
            topic=row["topic"],
            source=BusAddress(agent_id=row["source_agent_id"], group=row["source_group"]),
            target=target,
            payload=json.loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            correlation_id=row["correlation_id"],
            reply_to=row["reply_to"],
            ttl_ms=row["ttl_ms"],
            priority=row["priority"],
            requires_ack=bool(row["requires_ack"]),
            ack_level=AckLevel(row["ack_level"]),
        )

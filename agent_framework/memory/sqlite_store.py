from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent_framework.models.memory import MemoryKind, MemoryRecord

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS saved_memories (
    memory_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    user_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '[]',
    is_active INTEGER NOT NULL DEFAULT 1,
    is_pinned INTEGER NOT NULL DEFAULT 0,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    extra TEXT
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mem_user ON saved_memories (agent_id, user_id, is_active);",
    "CREATE INDEX IF NOT EXISTS idx_mem_kind ON saved_memories (agent_id, user_id, kind);",
    "CREATE INDEX IF NOT EXISTS idx_mem_updated ON saved_memories (agent_id, user_id, updated_at DESC);",
]


class SQLiteMemoryStore:
    """Default SQLite-backed memory store."""

    def __init__(self, db_path: str = "data/memories.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        cur = self._conn.cursor()
        cur.execute(_CREATE_TABLE)
        for idx_sql in _CREATE_INDEXES:
            cur.execute(idx_sql)
        self._conn.commit()

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            memory_id=row["memory_id"],
            agent_id=row["agent_id"],
            user_id=row["user_id"],
            kind=MemoryKind(row["kind"]),
            title=row["title"],
            content=row["content"],
            tags=json.loads(row["tags"]),
            is_active=bool(row["is_active"]),
            is_pinned=bool(row["is_pinned"]),
            source=row["source"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            last_used_at=(
                datetime.fromisoformat(row["last_used_at"])
                if row["last_used_at"]
                else None
            ),
            use_count=row["use_count"],
            version=row["version"],
            extra=json.loads(row["extra"]) if row["extra"] else None,
        )

    def save(self, record: MemoryRecord) -> str:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT INTO saved_memories
               (memory_id, agent_id, user_id, kind, title, content, tags,
                is_active, is_pinned, source, created_at, updated_at,
                last_used_at, use_count, version, extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.memory_id,
                record.agent_id,
                record.user_id,
                record.kind.value,
                record.title,
                record.content,
                json.dumps(record.tags),
                int(record.is_active),
                int(record.is_pinned),
                record.source,
                record.created_at.isoformat() if record.created_at else now,
                now,
                record.last_used_at.isoformat() if record.last_used_at else None,
                record.use_count,
                record.version,
                json.dumps(record.extra) if record.extra else None,
            ),
        )
        self._conn.commit()
        return record.memory_id

    def update(self, record: MemoryRecord) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """UPDATE saved_memories SET
               kind=?, title=?, content=?, tags=?, is_active=?, is_pinned=?,
               source=?, updated_at=?, last_used_at=?, use_count=?, version=?, extra=?
               WHERE memory_id=?""",
            (
                record.kind.value,
                record.title,
                record.content,
                json.dumps(record.tags),
                int(record.is_active),
                int(record.is_pinned),
                record.source,
                now,
                record.last_used_at.isoformat() if record.last_used_at else None,
                record.use_count,
                record.version,
                json.dumps(record.extra) if record.extra else None,
                record.memory_id,
            ),
        )
        self._conn.commit()

    def delete(self, memory_id: str) -> None:
        self._conn.execute(
            "DELETE FROM saved_memories WHERE memory_id=?", (memory_id,)
        )
        self._conn.commit()

    def get(self, memory_id: str) -> MemoryRecord | None:
        cur = self._conn.execute(
            "SELECT * FROM saved_memories WHERE memory_id=?", (memory_id,)
        )
        row = cur.fetchone()
        return self._row_to_record(row) if row else None

    def list_by_user(
        self, agent_id: str, user_id: str | None, active_only: bool = True
    ) -> list[MemoryRecord]:
        if active_only:
            sql = "SELECT * FROM saved_memories WHERE agent_id=? AND (user_id=? OR user_id IS NULL) AND is_active=1 ORDER BY updated_at DESC"
        else:
            sql = "SELECT * FROM saved_memories WHERE agent_id=? AND (user_id=? OR user_id IS NULL) ORDER BY updated_at DESC"
        cur = self._conn.execute(sql, (agent_id, user_id))
        return [self._row_to_record(r) for r in cur.fetchall()]

    def list_by_kind(
        self, agent_id: str, user_id: str | None, kind: MemoryKind
    ) -> list[MemoryRecord]:
        cur = self._conn.execute(
            "SELECT * FROM saved_memories WHERE agent_id=? AND (user_id=? OR user_id IS NULL) AND kind=? ORDER BY updated_at DESC",
            (agent_id, user_id, kind.value),
        )
        return [self._row_to_record(r) for r in cur.fetchall()]

    def list_recent(
        self, agent_id: str, user_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        cur = self._conn.execute(
            "SELECT * FROM saved_memories WHERE agent_id=? AND (user_id=? OR user_id IS NULL) AND is_active=1 ORDER BY updated_at DESC LIMIT ?",
            (agent_id, user_id, limit),
        )
        return [self._row_to_record(r) for r in cur.fetchall()]

    def touch(self, memory_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE saved_memories SET last_used_at=?, use_count=use_count+1 WHERE memory_id=?",
            (now, memory_id),
        )
        self._conn.commit()

    def count(self, agent_id: str, user_id: str | None) -> int:
        cur = self._conn.execute(
            "SELECT COUNT(*) FROM saved_memories WHERE agent_id=? AND (user_id=? OR user_id IS NULL)",
            (agent_id, user_id),
        )
        return cur.fetchone()[0]

    def close(self) -> None:
        self._conn.close()

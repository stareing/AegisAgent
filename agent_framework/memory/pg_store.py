"""PostgreSQL-backed memory store.

Requires: pip install psycopg2-binary  (or asyncpg for async usage)
Uses synchronous psycopg2 to match MemoryStoreProtocol's sync interface.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_framework.models.memory import MemoryKind, MemoryRecord
from agent_framework.models.message import Message

_CREATE_MEMORIES = """
CREATE TABLE IF NOT EXISTS saved_memories (
    memory_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    user_id TEXT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]',
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    is_pinned BOOLEAN NOT NULL DEFAULT FALSE,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    use_count INTEGER NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 1,
    extra JSONB
);
"""

_CREATE_CONVERSATION = """
CREATE TABLE IF NOT EXISTS conversation_history (
    id SERIAL PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    message_json TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);
"""

_CREATE_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_mem_user ON saved_memories (agent_id, user_id, is_active);",
    "CREATE INDEX IF NOT EXISTS idx_mem_kind ON saved_memories (agent_id, user_id, kind);",
    "CREATE INDEX IF NOT EXISTS idx_mem_updated ON saved_memories (agent_id, user_id, updated_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_conv_proj_id ON conversation_history (project_id, conversation_id);",
]


class PostgreSQLMemoryStore:
    """PostgreSQL-backed memory store implementing MemoryStoreProtocol."""

    def __init__(self, connection_url: str) -> None:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError:
            raise ImportError("psycopg2 not installed. Install with: pip install psycopg2-binary")

        self._conn = psycopg2.connect(connection_url)
        self._conn.autocommit = False
        psycopg2.extras.register_default_jsonb(self._conn)
        self._init_db()

    def _init_db(self) -> None:
        with self._conn.cursor() as cur:
            cur.execute(_CREATE_MEMORIES)
            cur.execute(_CREATE_CONVERSATION)
            for idx_sql in _CREATE_INDEXES:
                cur.execute(idx_sql)
        self._conn.commit()

    def _row_to_record(self, row: tuple, columns: list[str]) -> MemoryRecord:
        d = dict(zip(columns, row))
        tags = d["tags"] if isinstance(d["tags"], list) else json.loads(d["tags"] or "[]")
        return MemoryRecord(
            memory_id=d["memory_id"],
            agent_id=d["agent_id"],
            user_id=d["user_id"],
            kind=MemoryKind(d["kind"]),
            title=d["title"],
            content=d["content"],
            tags=tags,
            is_active=bool(d["is_active"]),
            is_pinned=bool(d["is_pinned"]),
            source=d["source"],
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            last_used_at=d["last_used_at"],
            use_count=d["use_count"],
            version=d["version"],
            extra=d["extra"],
        )

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _query_with_columns(self, sql: str, params: tuple = ()) -> tuple[list[tuple], list[str]]:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            columns = [desc[0] for desc in cur.description] if cur.description else []
            return cur.fetchall(), columns

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
        self._conn.commit()

    # ── memory CRUD ──────────────────────────────────────────

    def save(self, record: MemoryRecord) -> str:
        now = datetime.now(timezone.utc)
        self._execute(
            """INSERT INTO saved_memories
               (memory_id, agent_id, user_id, kind, title, content, tags,
                is_active, is_pinned, source, created_at, updated_at,
                last_used_at, use_count, version, extra)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                record.memory_id, record.agent_id, record.user_id,
                record.kind.value, record.title, record.content,
                json.dumps(record.tags), record.is_active, record.is_pinned,
                record.source, record.created_at or now, now,
                record.last_used_at, record.use_count, record.version,
                json.dumps(record.extra) if record.extra else None,
            ),
        )
        return record.memory_id

    def update(self, record: MemoryRecord) -> None:
        now = datetime.now(timezone.utc)
        self._execute(
            """UPDATE saved_memories SET
               kind=%s, title=%s, content=%s, tags=%s, is_active=%s, is_pinned=%s,
               source=%s, updated_at=%s, last_used_at=%s, use_count=%s, version=%s, extra=%s
               WHERE memory_id=%s""",
            (
                record.kind.value, record.title, record.content,
                json.dumps(record.tags), record.is_active, record.is_pinned,
                record.source, now, record.last_used_at,
                record.use_count, record.version,
                json.dumps(record.extra) if record.extra else None,
                record.memory_id,
            ),
        )

    def delete(self, memory_id: str) -> None:
        self._execute("DELETE FROM saved_memories WHERE memory_id=%s", (memory_id,))

    def get(self, memory_id: str) -> MemoryRecord | None:
        rows, cols = self._query_with_columns(
            "SELECT * FROM saved_memories WHERE memory_id=%s", (memory_id,)
        )
        return self._row_to_record(rows[0], cols) if rows else None

    def list_by_user(
        self, agent_id: str, user_id: str | None, active_only: bool = True
    ) -> list[MemoryRecord]:
        sql = "SELECT * FROM saved_memories WHERE agent_id=%s AND (user_id=%s OR user_id IS NULL)"
        if active_only:
            sql += " AND is_active=TRUE"
        sql += " ORDER BY updated_at DESC"
        rows, cols = self._query_with_columns(sql, (agent_id, user_id))
        return [self._row_to_record(r, cols) for r in rows]

    def list_by_kind(
        self, agent_id: str, user_id: str | None, kind: MemoryKind
    ) -> list[MemoryRecord]:
        rows, cols = self._query_with_columns(
            "SELECT * FROM saved_memories WHERE agent_id=%s AND (user_id=%s OR user_id IS NULL) AND kind=%s ORDER BY updated_at DESC",
            (agent_id, user_id, kind.value),
        )
        return [self._row_to_record(r, cols) for r in rows]

    def list_recent(
        self, agent_id: str, user_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        rows, cols = self._query_with_columns(
            "SELECT * FROM saved_memories WHERE agent_id=%s AND (user_id=%s OR user_id IS NULL) AND is_active=TRUE ORDER BY updated_at DESC LIMIT %s",
            (agent_id, user_id, limit),
        )
        return [self._row_to_record(r, cols) for r in rows]

    def touch(self, memory_id: str) -> None:
        self._execute(
            "UPDATE saved_memories SET last_used_at=%s, use_count=use_count+1 WHERE memory_id=%s",
            (datetime.now(timezone.utc), memory_id),
        )

    def count(self, agent_id: str, user_id: str | None) -> int:
        rows = self._query(
            "SELECT COUNT(*) FROM saved_memories WHERE agent_id=%s AND (user_id=%s OR user_id IS NULL)",
            (agent_id, user_id),
        )
        return rows[0][0]

    # ── conversation history ─────────────────────────────────

    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    def save_conversation(
        self, project_id: str, conversation_id: str, messages: list[Message],
    ) -> None:
        now = datetime.now(timezone.utc)
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM conversation_history WHERE conversation_id=%s", (conversation_id,))
            for seq, msg in enumerate(messages):
                cur.execute(
                    "INSERT INTO conversation_history (conversation_id, project_id, seq, message_json, created_at) VALUES (%s, %s, %s, %s, %s)",
                    (conversation_id, project_id, seq, msg.model_dump_json(), now),
                )
        self._conn.commit()

    def load_conversation(self, conversation_id: str) -> list[Message]:
        rows = self._query(
            "SELECT message_json FROM conversation_history WHERE conversation_id=%s ORDER BY seq",
            (conversation_id,),
        )
        messages: list[Message] = []
        for row in rows:
            try:
                messages.append(Message.model_validate_json(row[0]))
            except Exception:
                continue
        return messages

    def get_latest_conversation_id(self, project_id: str) -> str | None:
        rows = self._query(
            "SELECT conversation_id FROM conversation_history WHERE project_id=%s ORDER BY id DESC LIMIT 1",
            (project_id,),
        )
        return rows[0][0] if rows else None

    def list_conversations(self, project_id: str) -> list[dict]:
        rows = self._query(
            "SELECT conversation_id, MIN(created_at) as created_at, COUNT(*) as msg_count "
            "FROM conversation_history WHERE project_id=%s "
            "GROUP BY conversation_id ORDER BY created_at DESC",
            (project_id,),
        )
        results = []
        for conv_id, created_at, msg_count in rows:
            first = self._query(
                "SELECT message_json FROM conversation_history WHERE conversation_id=%s ORDER BY seq LIMIT 1",
                (conv_id,),
            )
            preview = ""
            if first:
                try:
                    msg = Message.model_validate_json(first[0][0])
                    preview = (msg.content or "")[:60]
                except Exception:
                    pass
            results.append({
                "conversation_id": conv_id,
                "message_count": msg_count,
                "created_at": str(created_at),
                "preview": preview,
            })
        return results

    def clear_conversation(self, conversation_id: str) -> None:
        self._execute("DELETE FROM conversation_history WHERE conversation_id=%s", (conversation_id,))

    def close(self) -> None:
        self._conn.close()

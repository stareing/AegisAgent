"""Neo4j-backed memory store.

Requires: pip install neo4j
Stores memories as nodes, conversations as linked chains.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_framework.models.memory import MemoryKind, MemoryRecord
from agent_framework.models.message import Message


class Neo4jMemoryStore:
    """Neo4j-backed memory store implementing MemoryStoreProtocol."""

    def __init__(self, connection_url: str, auth: tuple[str, str] = ("neo4j", "neo4j"), database: str = "neo4j") -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError:
            raise ImportError("neo4j not installed. Install with: pip install neo4j")

        self._driver = GraphDatabase.driver(connection_url, auth=auth)
        self._database = database
        self._init_constraints()

    def _init_constraints(self) -> None:
        with self._driver.session(database=self._database) as s:
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.memory_id IS UNIQUE")
            s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (c:ConversationMessage) REQUIRE c.uid IS UNIQUE")
            s.run("CREATE INDEX IF NOT EXISTS FOR (m:Memory) ON (m.agent_id, m.user_id, m.is_active)")
            s.run("CREATE INDEX IF NOT EXISTS FOR (c:ConversationMessage) ON (c.project_id, c.conversation_id)")

    def _run(self, query: str, **params: Any) -> list[dict]:
        with self._driver.session(database=self._database) as s:
            result = s.run(query, **params)
            return [r.data() for r in result]

    @staticmethod
    def _record_to_props(record: MemoryRecord) -> dict:
        return {
            "memory_id": record.memory_id,
            "agent_id": record.agent_id,
            "user_id": record.user_id,
            "kind": record.kind.value,
            "title": record.title,
            "content": record.content,
            "tags": json.dumps(record.tags),
            "is_active": record.is_active,
            "is_pinned": record.is_pinned,
            "source": record.source,
            "created_at": record.created_at.isoformat() if record.created_at else datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_used_at": record.last_used_at.isoformat() if record.last_used_at else None,
            "use_count": record.use_count,
            "version": record.version,
            "extra": json.dumps(record.extra) if record.extra else None,
        }

    @staticmethod
    def _props_to_record(props: dict) -> MemoryRecord:
        return MemoryRecord(
            memory_id=props["memory_id"],
            agent_id=props["agent_id"],
            user_id=props.get("user_id"),
            kind=MemoryKind(props["kind"]),
            title=props["title"],
            content=props["content"],
            tags=json.loads(props.get("tags", "[]")),
            is_active=props.get("is_active", True),
            is_pinned=props.get("is_pinned", False),
            source=props.get("source"),
            created_at=datetime.fromisoformat(props["created_at"]),
            updated_at=datetime.fromisoformat(props["updated_at"]),
            last_used_at=datetime.fromisoformat(props["last_used_at"]) if props.get("last_used_at") else None,
            use_count=props.get("use_count", 0),
            version=props.get("version", 1),
            extra=json.loads(props["extra"]) if props.get("extra") else None,
        )

    # ── memory CRUD ──────────────────────────────────────────

    def save(self, record: MemoryRecord) -> str:
        props = self._record_to_props(record)
        self._run("CREATE (m:Memory $props)", props=props)
        return record.memory_id

    def update(self, record: MemoryRecord) -> None:
        props = self._record_to_props(record)
        mid = props.pop("memory_id")
        self._run("MATCH (m:Memory {memory_id: $mid}) SET m += $props", mid=mid, props=props)

    def delete(self, memory_id: str) -> None:
        self._run("MATCH (m:Memory {memory_id: $mid}) DETACH DELETE m", mid=memory_id)

    def get(self, memory_id: str) -> MemoryRecord | None:
        rows = self._run("MATCH (m:Memory {memory_id: $mid}) RETURN m", mid=memory_id)
        return self._props_to_record(rows[0]["m"]) if rows else None

    def list_by_user(
        self, agent_id: str, user_id: str | None, active_only: bool = True
    ) -> list[MemoryRecord]:
        q = "MATCH (m:Memory) WHERE m.agent_id=$aid AND (m.user_id=$uid OR m.user_id IS NULL)"
        if active_only:
            q += " AND m.is_active=true"
        q += " RETURN m ORDER BY m.updated_at DESC"
        return [self._props_to_record(r["m"]) for r in self._run(q, aid=agent_id, uid=user_id)]

    def list_by_kind(
        self, agent_id: str, user_id: str | None, kind: MemoryKind
    ) -> list[MemoryRecord]:
        return [self._props_to_record(r["m"]) for r in self._run(
            "MATCH (m:Memory) WHERE m.agent_id=$aid AND (m.user_id=$uid OR m.user_id IS NULL) AND m.kind=$kind "
            "RETURN m ORDER BY m.updated_at DESC",
            aid=agent_id, uid=user_id, kind=kind.value,
        )]

    def list_recent(
        self, agent_id: str, user_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        return [self._props_to_record(r["m"]) for r in self._run(
            "MATCH (m:Memory) WHERE m.agent_id=$aid AND (m.user_id=$uid OR m.user_id IS NULL) AND m.is_active=true "
            "RETURN m ORDER BY m.updated_at DESC LIMIT $lim",
            aid=agent_id, uid=user_id, lim=limit,
        )]

    def touch(self, memory_id: str) -> None:
        self._run(
            "MATCH (m:Memory {memory_id: $mid}) SET m.last_used_at=$now, m.use_count=m.use_count+1",
            mid=memory_id, now=datetime.now(timezone.utc).isoformat(),
        )

    def count(self, agent_id: str, user_id: str | None) -> int:
        rows = self._run(
            "MATCH (m:Memory) WHERE m.agent_id=$aid AND (m.user_id=$uid OR m.user_id IS NULL) RETURN count(m) AS c",
            aid=agent_id, uid=user_id,
        )
        return rows[0]["c"] if rows else 0

    # ── conversation history ─────────────────────────────────

    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    def save_conversation(
        self, project_id: str, conversation_id: str, messages: list[Message],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._run("MATCH (c:ConversationMessage {conversation_id: $cid}) DETACH DELETE c", cid=conversation_id)
        for seq, msg in enumerate(messages):
            self._run(
                "CREATE (c:ConversationMessage {uid: $uid, conversation_id: $cid, project_id: $pid, "
                "seq: $seq, message_json: $mj, created_at: $now})",
                uid=f"{conversation_id}:{seq}",
                cid=conversation_id, pid=project_id,
                seq=seq, mj=msg.model_dump_json(), now=now,
            )

    def load_conversation(self, conversation_id: str) -> list[Message]:
        rows = self._run(
            "MATCH (c:ConversationMessage {conversation_id: $cid}) RETURN c.message_json AS mj ORDER BY c.seq",
            cid=conversation_id,
        )
        messages: list[Message] = []
        for r in rows:
            try:
                messages.append(Message.model_validate_json(r["mj"]))
            except Exception:
                continue
        return messages

    def get_latest_conversation_id(self, project_id: str) -> str | None:
        rows = self._run(
            "MATCH (c:ConversationMessage {project_id: $pid}) "
            "RETURN c.conversation_id AS cid ORDER BY c.created_at DESC LIMIT 1",
            pid=project_id,
        )
        return rows[0]["cid"] if rows else None

    def list_conversations(self, project_id: str) -> list[dict]:
        rows = self._run(
            "MATCH (c:ConversationMessage {project_id: $pid}) "
            "WITH c.conversation_id AS cid, min(c.created_at) AS ca, count(c) AS cnt "
            "RETURN cid, ca, cnt ORDER BY ca DESC",
            pid=project_id,
        )
        results = []
        for r in rows:
            conv_id = r["cid"]
            first = self._run(
                "MATCH (c:ConversationMessage {conversation_id: $cid}) RETURN c.message_json AS mj ORDER BY c.seq LIMIT 1",
                cid=conv_id,
            )
            preview = ""
            if first:
                try:
                    msg = Message.model_validate_json(first[0]["mj"])
                    preview = (msg.content or "")[:60]
                except Exception:
                    pass
            results.append({
                "conversation_id": conv_id,
                "message_count": r["cnt"],
                "created_at": r["ca"],
                "preview": preview,
            })
        return results

    def clear_conversation(self, conversation_id: str) -> None:
        self._run("MATCH (c:ConversationMessage {conversation_id: $cid}) DETACH DELETE c", cid=conversation_id)

    def close(self) -> None:
        self._driver.close()

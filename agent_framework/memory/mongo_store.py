"""MongoDB-backed memory store.

Requires: pip install pymongo
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from agent_framework.models.memory import MemoryKind, MemoryRecord
from agent_framework.models.message import Message


class MongoDBMemoryStore:
    """MongoDB-backed memory store implementing MemoryStoreProtocol."""

    def __init__(self, connection_url: str, database_name: str = "agent_memory") -> None:
        try:
            from pymongo import MongoClient
        except ImportError:
            raise ImportError("pymongo not installed. Install with: pip install pymongo")

        self._client = MongoClient(connection_url)
        self._db = self._client[database_name]
        self._memories = self._db["saved_memories"]
        self._conversations = self._db["conversation_history"]
        self._init_indexes()

    def _init_indexes(self) -> None:
        self._memories.create_index([("agent_id", 1), ("user_id", 1), ("is_active", 1)])
        self._memories.create_index([("agent_id", 1), ("user_id", 1), ("kind", 1)])
        self._memories.create_index([("agent_id", 1), ("user_id", 1), ("updated_at", -1)])
        self._conversations.create_index([("project_id", 1), ("conversation_id", 1)])
        self._conversations.create_index([("conversation_id", 1), ("seq", 1)])

    @staticmethod
    def _record_to_doc(record: MemoryRecord) -> dict:
        return {
            "_id": record.memory_id,
            "agent_id": record.agent_id,
            "user_id": record.user_id,
            "kind": record.kind.value,
            "title": record.title,
            "content": record.content,
            "tags": record.tags,
            "is_active": record.is_active,
            "is_pinned": record.is_pinned,
            "source": record.source,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "last_used_at": record.last_used_at,
            "use_count": record.use_count,
            "version": record.version,
            "extra": record.extra,
        }

    @staticmethod
    def _doc_to_record(doc: dict) -> MemoryRecord:
        return MemoryRecord(
            memory_id=doc["_id"],
            agent_id=doc["agent_id"],
            user_id=doc.get("user_id"),
            kind=MemoryKind(doc["kind"]),
            title=doc["title"],
            content=doc["content"],
            tags=doc.get("tags", []),
            is_active=doc.get("is_active", True),
            is_pinned=doc.get("is_pinned", False),
            source=doc.get("source"),
            created_at=doc.get("created_at", datetime.now(timezone.utc)),
            updated_at=doc.get("updated_at", datetime.now(timezone.utc)),
            last_used_at=doc.get("last_used_at"),
            use_count=doc.get("use_count", 0),
            version=doc.get("version", 1),
            extra=doc.get("extra"),
        )

    # ── memory CRUD ──────────────────────────────────────────

    def save(self, record: MemoryRecord) -> str:
        now = datetime.now(timezone.utc)
        doc = self._record_to_doc(record)
        doc["updated_at"] = now
        if not doc.get("created_at"):
            doc["created_at"] = now
        self._memories.insert_one(doc)
        return record.memory_id

    def update(self, record: MemoryRecord) -> None:
        now = datetime.now(timezone.utc)
        self._memories.update_one(
            {"_id": record.memory_id},
            {"$set": {
                "kind": record.kind.value,
                "title": record.title,
                "content": record.content,
                "tags": record.tags,
                "is_active": record.is_active,
                "is_pinned": record.is_pinned,
                "source": record.source,
                "updated_at": now,
                "last_used_at": record.last_used_at,
                "use_count": record.use_count,
                "version": record.version,
                "extra": record.extra,
            }},
        )

    def delete(self, memory_id: str) -> None:
        self._memories.delete_one({"_id": memory_id})

    def get(self, memory_id: str) -> MemoryRecord | None:
        doc = self._memories.find_one({"_id": memory_id})
        return self._doc_to_record(doc) if doc else None

    def list_by_user(
        self, agent_id: str, user_id: str | None, active_only: bool = True
    ) -> list[MemoryRecord]:
        query: dict[str, Any] = {
            "agent_id": agent_id,
            "$or": [{"user_id": user_id}, {"user_id": None}],
        }
        if active_only:
            query["is_active"] = True
        docs = self._memories.find(query).sort("updated_at", -1)
        return [self._doc_to_record(d) for d in docs]

    def list_by_kind(
        self, agent_id: str, user_id: str | None, kind: MemoryKind
    ) -> list[MemoryRecord]:
        docs = self._memories.find({
            "agent_id": agent_id,
            "$or": [{"user_id": user_id}, {"user_id": None}],
            "kind": kind.value,
        }).sort("updated_at", -1)
        return [self._doc_to_record(d) for d in docs]

    def list_recent(
        self, agent_id: str, user_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        docs = self._memories.find({
            "agent_id": agent_id,
            "$or": [{"user_id": user_id}, {"user_id": None}],
            "is_active": True,
        }).sort("updated_at", -1).limit(limit)
        return [self._doc_to_record(d) for d in docs]

    def touch(self, memory_id: str) -> None:
        self._memories.update_one(
            {"_id": memory_id},
            {"$set": {"last_used_at": datetime.now(timezone.utc)}, "$inc": {"use_count": 1}},
        )

    def count(self, agent_id: str, user_id: str | None) -> int:
        return self._memories.count_documents({
            "agent_id": agent_id,
            "$or": [{"user_id": user_id}, {"user_id": None}],
        })

    # ── conversation history ─────────────────────────────────

    def new_conversation_id(self) -> str:
        return str(uuid.uuid4())

    def save_conversation(
        self, project_id: str, conversation_id: str, messages: list[Message],
    ) -> None:
        now = datetime.now(timezone.utc)
        self._conversations.delete_many({"conversation_id": conversation_id})
        if messages:
            docs = [
                {
                    "conversation_id": conversation_id,
                    "project_id": project_id,
                    "seq": seq,
                    "message_json": msg.model_dump_json(),
                    "created_at": now,
                }
                for seq, msg in enumerate(messages)
            ]
            self._conversations.insert_many(docs)

    def load_conversation(self, conversation_id: str) -> list[Message]:
        docs = self._conversations.find(
            {"conversation_id": conversation_id}
        ).sort("seq", 1)
        messages: list[Message] = []
        for doc in docs:
            try:
                messages.append(Message.model_validate_json(doc["message_json"]))
            except Exception:
                continue
        return messages

    def get_latest_conversation_id(self, project_id: str) -> str | None:
        doc = self._conversations.find_one(
            {"project_id": project_id},
            sort=[("_id", -1)],
            projection={"conversation_id": 1},
        )
        return doc["conversation_id"] if doc else None

    def list_conversations(self, project_id: str) -> list[dict]:
        pipeline = [
            {"$match": {"project_id": project_id}},
            {"$group": {
                "_id": "$conversation_id",
                "created_at": {"$min": "$created_at"},
                "msg_count": {"$sum": 1},
            }},
            {"$sort": {"created_at": -1}},
        ]
        results = []
        for agg in self._conversations.aggregate(pipeline):
            conv_id = agg["_id"]
            first = self._conversations.find_one(
                {"conversation_id": conv_id},
                sort=[("seq", 1)],
                projection={"message_json": 1},
            )
            preview = ""
            if first:
                try:
                    msg = Message.model_validate_json(first["message_json"])
                    preview = (msg.content or "")[:60]
                except Exception:
                    pass
            results.append({
                "conversation_id": conv_id,
                "message_count": agg["msg_count"],
                "created_at": str(agg["created_at"]),
                "preview": preview,
            })
        return results

    def clear_conversation(self, conversation_id: str) -> None:
        self._conversations.delete_many({"conversation_id": conversation_id})

    def close(self) -> None:
        self._client.close()

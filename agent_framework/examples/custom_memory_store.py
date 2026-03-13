"""Example: Custom memory store implementation.

Demonstrates how to replace the default SQLite store with a custom one
while keeping the DefaultMemoryManager (replacement method A, section 11.9).
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_framework.models.memory import MemoryKind, MemoryRecord


class JsonFileMemoryStore:
    """A simple JSON-file-based memory store.

    Implements MemoryStoreProtocol using a single JSON file for persistence.
    Suitable for small-scale local usage.
    """

    def __init__(self, file_path: str = "data/memories.json") -> None:
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, dict] = {}
        if self._path.exists():
            self._data = json.loads(self._path.read_text())

    def _persist(self) -> None:
        self._path.write_text(json.dumps(self._data, ensure_ascii=False, indent=2))

    def save(self, record: MemoryRecord) -> str:
        self._data[record.memory_id] = record.model_dump(mode="json")
        self._persist()
        return record.memory_id

    def update(self, record: MemoryRecord) -> None:
        self._data[record.memory_id] = record.model_dump(mode="json")
        self._persist()

    def delete(self, memory_id: str) -> None:
        self._data.pop(memory_id, None)
        self._persist()

    def get(self, memory_id: str) -> MemoryRecord | None:
        raw = self._data.get(memory_id)
        if raw is None:
            return None
        return MemoryRecord(**raw)

    def list_by_user(
        self, agent_id: str, user_id: str | None, active_only: bool = True
    ) -> list[MemoryRecord]:
        results = []
        for raw in self._data.values():
            if raw["agent_id"] != agent_id:
                continue
            if user_id is not None and raw.get("user_id") != user_id:
                continue
            if active_only and not raw.get("is_active", True):
                continue
            results.append(MemoryRecord(**raw))
        return results

    def list_by_kind(
        self, agent_id: str, user_id: str | None, kind: MemoryKind
    ) -> list[MemoryRecord]:
        return [
            r for r in self.list_by_user(agent_id, user_id, active_only=False)
            if r.kind == kind
        ]

    def list_recent(
        self, agent_id: str, user_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        all_records = self.list_by_user(agent_id, user_id, active_only=False)
        all_records.sort(key=lambda r: r.updated_at, reverse=True)
        return all_records[:limit]

    def touch(self, memory_id: str) -> None:
        raw = self._data.get(memory_id)
        if raw:
            from datetime import datetime, timezone
            raw["last_used_at"] = datetime.now(timezone.utc).isoformat()
            raw["use_count"] = raw.get("use_count", 0) + 1
            self._persist()

    def count(self, agent_id: str, user_id: str | None) -> int:
        return len(self.list_by_user(agent_id, user_id, active_only=False))


# Usage:
# from agent_framework.memory.default_manager import DefaultMemoryManager
# store = JsonFileMemoryStore("data/my_memories.json")
# manager = DefaultMemoryManager(store=store)

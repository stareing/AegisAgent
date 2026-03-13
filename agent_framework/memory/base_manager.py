from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from agent_framework.models.memory import (
    MemoryCandidate,
    MemoryRecord,
    MemoryUpdateAction,
)

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentState, IterationResult
    from agent_framework.protocols.core import MemoryStoreProtocol


class BaseMemoryManager(ABC):
    """Base class for memory management.

    Responsibilities:
    - Decide what is worth remembering
    - Decide whether to update, overwrite, or ignore
    - Decide which memories enter the current context
    - Provide user governance interfaces

    Does NOT handle:
    - Prompt formatting (context layer's job)
    - Session history management
    - Model calls
    - Tool execution
    """

    def __init__(self, store: MemoryStoreProtocol) -> None:
        self._store = store
        self._enabled = True
        self._run_id: str | None = None
        self._agent_id: str | None = None
        self._user_id: str | None = None

    def begin_session(
        self, run_id: str, agent_id: str, user_id: str | None
    ) -> None:
        self._run_id = run_id
        self._agent_id = agent_id
        self._user_id = user_id

    @abstractmethod
    def select_for_context(
        self, task: str, agent_state: AgentState
    ) -> list[MemoryRecord]:
        """Select memories to inject into the current context."""
        ...

    @abstractmethod
    def record_turn(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> None:
        """Process a completed turn for potential memory extraction."""
        ...

    @abstractmethod
    def extract_candidates(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> list[MemoryCandidate]:
        """Extract memory candidates from a turn."""
        ...

    @abstractmethod
    def merge_candidate(
        self,
        candidate: MemoryCandidate,
        existing_records: list[MemoryRecord],
    ) -> MemoryUpdateAction:
        """Decide how to handle a candidate against existing records."""
        ...

    def remember(self, candidate: MemoryCandidate) -> str | None:
        """Save or update a memory from a candidate."""
        if not self._enabled or not self._agent_id:
            return None

        existing = self._store.list_by_user(
            self._agent_id, self._user_id, active_only=False
        )
        action = self.merge_candidate(candidate, existing)

        if action == MemoryUpdateAction.IGNORE:
            return None

        if action == MemoryUpdateAction.DELETE:
            # Find and delete matching
            for r in existing:
                if self._normalize(r.title) == self._normalize(candidate.title):
                    self._store.delete(r.memory_id)
            return None

        # UPSERT
        import uuid

        match = None
        for r in existing:
            if (
                r.kind == candidate.kind
                and self._normalize(r.title) == self._normalize(candidate.title)
            ):
                match = r
                break

        if match:
            if match.is_pinned:
                return match.memory_id
            match.content = candidate.content
            match.tags = candidate.tags
            match.version += 1
            self._store.update(match)
            return match.memory_id
        else:
            record = MemoryRecord(
                memory_id=str(uuid.uuid4()),
                agent_id=self._agent_id,
                user_id=self._user_id,
                kind=candidate.kind,
                title=candidate.title,
                content=candidate.content,
                tags=candidate.tags,
                source="auto",
            )
            return self._store.save(record)

    def forget(self, memory_id: str) -> None:
        self._store.delete(memory_id)

    def list_memories(
        self, agent_id: str, user_id: str | None
    ) -> list[MemoryRecord]:
        return self._store.list_by_user(agent_id, user_id, active_only=False)

    def pin(self, memory_id: str) -> None:
        record = self._store.get(memory_id)
        if record:
            record.is_pinned = True
            self._store.update(record)

    def unpin(self, memory_id: str) -> None:
        record = self._store.get(memory_id)
        if record:
            record.is_pinned = False
            self._store.update(record)

    def activate(self, memory_id: str) -> None:
        record = self._store.get(memory_id)
        if record:
            record.is_active = True
            self._store.update(record)

    def deactivate(self, memory_id: str) -> None:
        record = self._store.get(memory_id)
        if record:
            record.is_active = False
            self._store.update(record)

    def clear_memories(self, agent_id: str, user_id: str | None) -> int:
        records = self._store.list_by_user(agent_id, user_id, active_only=False)
        for r in records:
            self._store.delete(r.memory_id)
        return len(records)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def end_session(self) -> None:
        self._run_id = None

    @staticmethod
    def _normalize(text: str) -> str:
        return text.strip().lower()

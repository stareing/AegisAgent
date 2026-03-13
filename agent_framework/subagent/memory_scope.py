from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.memory.base_manager import BaseMemoryManager
from agent_framework.models.memory import (
    MemoryCandidate,
    MemoryRecord,
    MemoryUpdateAction,
)

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentState, IterationResult
    from agent_framework.protocols.core import MemoryManagerProtocol, MemoryStoreProtocol


class IsolatedMemoryManager(BaseMemoryManager):
    """ISOLATED scope: subagent has its own empty memory, no parent access.

    Writes go to a separate namespace, reads return nothing from parent.
    """

    def __init__(self, store: MemoryStoreProtocol) -> None:
        super().__init__(store)

    def select_for_context(
        self, task: str, agent_state: AgentState
    ) -> list[MemoryRecord]:
        if not self._enabled or not self._agent_id:
            return []
        return self._store.list_by_user(self._agent_id, self._user_id, active_only=True)

    def record_turn(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> None:
        pass

    def extract_candidates(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> list[MemoryCandidate]:
        return []

    def merge_candidate(
        self, candidate: MemoryCandidate, existing_records: list[MemoryRecord]
    ) -> MemoryUpdateAction:
        return MemoryUpdateAction.UPSERT


class InheritReadMemoryManager(BaseMemoryManager):
    """INHERIT_READ scope: subagent can read parent memories, writes are local only.

    Parent memories are injected into context but subagent writes go to its own store.
    """

    def __init__(
        self,
        store: MemoryStoreProtocol,
        parent_manager: MemoryManagerProtocol,
        parent_agent_id: str,
        parent_user_id: str | None = None,
        max_inherited: int = 5,
    ) -> None:
        super().__init__(store)
        self._parent_manager = parent_manager
        self._parent_agent_id = parent_agent_id
        self._parent_user_id = parent_user_id
        self._max_inherited = max_inherited

    def select_for_context(
        self, task: str, agent_state: AgentState
    ) -> list[MemoryRecord]:
        own: list[MemoryRecord] = []
        if self._enabled and self._agent_id:
            own = self._store.list_by_user(
                self._agent_id, self._user_id, active_only=True
            )
        parent = self._parent_manager.select_for_context(task, agent_state)
        inherited = parent[: self._max_inherited]
        return inherited + own

    def record_turn(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> None:
        pass

    def extract_candidates(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> list[MemoryCandidate]:
        return []

    def merge_candidate(
        self, candidate: MemoryCandidate, existing_records: list[MemoryRecord]
    ) -> MemoryUpdateAction:
        return MemoryUpdateAction.UPSERT


class SharedWriteMemoryManager(BaseMemoryManager):
    """SHARED_WRITE scope: subagent reads and writes through parent's MemoryManager.

    Per doc 14.3: subagent never directly writes to parent store,
    only through parent MemoryManager.remember().
    """

    def __init__(
        self,
        parent_manager: MemoryManagerProtocol,
    ) -> None:
        self._parent = parent_manager
        super().__init__(None)  # type: ignore[arg-type]
        self._enabled = True

    def begin_session(
        self, run_id: str, agent_id: str, user_id: str | None
    ) -> None:
        self._run_id = run_id
        self._agent_id = agent_id
        self._user_id = user_id

    def select_for_context(
        self, task: str, agent_state: AgentState
    ) -> list[MemoryRecord]:
        return self._parent.select_for_context(task, agent_state)

    def record_turn(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> None:
        self._parent.record_turn(user_input, final_answer, iteration_results)

    def extract_candidates(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> list[MemoryCandidate]:
        return []

    def remember(self, candidate: MemoryCandidate) -> str | None:
        return self._parent.remember(candidate)

    def forget(self, memory_id: str) -> None:
        self._parent.forget(memory_id)

    def merge_candidate(
        self, candidate: MemoryCandidate, existing_records: list[MemoryRecord]
    ) -> MemoryUpdateAction:
        return MemoryUpdateAction.UPSERT

    def end_session(self) -> None:
        pass

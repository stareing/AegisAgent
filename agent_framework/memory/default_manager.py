from __future__ import annotations

import re
from typing import TYPE_CHECKING

from agent_framework.memory.base_manager import BaseMemoryManager
from agent_framework.models.memory import (
    MemoryCandidate,
    MemoryKind,
    MemoryRecord,
    MemoryUpdateAction,
)

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentState, IterationResult
    from agent_framework.protocols.core import MemoryStoreProtocol

# Patterns that suggest a memory-worthy statement
_PREFERENCE_PATTERNS = [
    r"以后.*(?:用|使用|回答|输出)",
    r"(?:always|never|prefer|don't|do not)\b.*",
    r"我(?:喜欢|偏好|希望|想要|需要)",
    r"(?:remember|记住|注意).*",
]

_CONSTRAINT_PATTERNS = [
    r"不要.*(?:使用|用|做)",
    r"(?:禁止|避免|不可以|不允许)",
    r"must not|should not|do not use",
]

_PROJECT_PATTERNS = [
    r"(?:项目|project).*(?:是|关于|用于|做)",
    r"(?:我们|我)正在.*(?:开发|做|写|构建)",
    r"(?:tech stack|技术栈|框架)",
]


class DefaultMemoryManager(BaseMemoryManager):
    """Default memory manager implementing simple rule-based extraction.

    Follows GPT-style Saved Memory principles:
    - Save user preferences, constraints, project context
    - Don't save temporary questions, chat logs, tool outputs
    """

    def __init__(
        self,
        store: MemoryStoreProtocol,
        max_memories_in_context: int = 10,
        auto_extract: bool = True,
    ) -> None:
        super().__init__(store)
        self._max_in_context = max_memories_in_context
        self._auto_extract = auto_extract

    def select_for_context(
        self, task: str, agent_state: AgentState
    ) -> list[MemoryRecord]:
        """Select memories for the current context.

        Rules (section 11.7):
        1. Pinned memories first
        2. Keyword-matched memories
        3. Recently updated active memories
        4. Total limited to max_memories_in_context
        """
        if not self._enabled or not self._agent_id:
            return []

        all_active = self._store.list_by_user(
            self._agent_id, self._user_id, active_only=True
        )

        pinned = [m for m in all_active if m.is_pinned]
        non_pinned = [m for m in all_active if not m.is_pinned]

        # Simple keyword matching
        task_words = set(task.lower().split())
        scored: list[tuple[int, MemoryRecord]] = []
        for m in non_pinned:
            title_words = set(m.title.lower().split())
            content_words = set(m.content.lower().split())
            tag_words = set(t.lower() for t in m.tags)
            overlap = len(task_words & (title_words | content_words | tag_words))
            scored.append((overlap, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        matched = [m for _, m in scored]

        result = pinned + matched
        # Touch selected memories
        for m in result[: self._max_in_context]:
            self._store.touch(m.memory_id)

        return result[: self._max_in_context]

    def record_turn(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> None:
        if not self._enabled or not self._auto_extract:
            return
        candidates = self.extract_candidates(user_input, final_answer, iteration_results)
        for c in candidates:
            self.remember(c)

    def extract_candidates(
        self,
        user_input: str,
        final_answer: str | None,
        iteration_results: list[IterationResult],
    ) -> list[MemoryCandidate]:
        """Extract memory candidates using simple pattern matching."""
        candidates: list[MemoryCandidate] = []
        text = user_input.strip()
        if not text:
            return candidates

        # Check preference patterns
        for pattern in _PREFERENCE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                candidates.append(
                    MemoryCandidate(
                        kind=MemoryKind.USER_PREFERENCE,
                        title=self._make_title(text),
                        content=text,
                        tags=["preference"],
                        reason="User preference detected",
                    )
                )
                break

        # Check constraint patterns
        for pattern in _CONSTRAINT_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                candidates.append(
                    MemoryCandidate(
                        kind=MemoryKind.USER_CONSTRAINT,
                        title=self._make_title(text),
                        content=text,
                        tags=["constraint"],
                        reason="User constraint detected",
                    )
                )
                break

        # Check project patterns
        for pattern in _PROJECT_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                candidates.append(
                    MemoryCandidate(
                        kind=MemoryKind.PROJECT_CONTEXT,
                        title=self._make_title(text),
                        content=text,
                        tags=["project"],
                        reason="Project context detected",
                    )
                )
                break

        return candidates

    def merge_candidate(
        self,
        candidate: MemoryCandidate,
        existing_records: list[MemoryRecord],
    ) -> MemoryUpdateAction:
        """Decide how to handle a candidate (section 11.6).

        1. Same kind + normalized title -> update
        2. Same content exactly -> ignore
        3. Pinned records not auto-overwritten
        4. Conflict -> version bump
        """
        norm_title = self._normalize(candidate.title)

        for r in existing_records:
            # Rule 1: Same kind + normalized title -> update logic
            if r.kind == candidate.kind and self._normalize(r.title) == norm_title:
                # Rule 2: Same content exactly -> ignore
                if r.content.strip() == candidate.content.strip():
                    return MemoryUpdateAction.IGNORE
                # Rule 3: Pinned records not auto-overwritten
                if r.is_pinned:
                    return MemoryUpdateAction.IGNORE
                # Rule 4: Conflict -> version bump (UPSERT)
                return MemoryUpdateAction.UPSERT

            # Rule 2 also applies across different kind/title
            if r.content.strip() == candidate.content.strip():
                return MemoryUpdateAction.IGNORE

        return MemoryUpdateAction.UPSERT

    @staticmethod
    def _make_title(text: str, max_len: int = 50) -> str:
        """Create a short title from text."""
        clean = text.replace("\n", " ").strip()
        if len(clean) <= max_len:
            return clean
        return clean[:max_len].rsplit(" ", 1)[0] + "..."

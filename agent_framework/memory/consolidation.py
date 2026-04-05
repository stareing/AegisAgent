"""Memory consolidation — LLM-based dream agent callback.

Reviews recent session memories and consolidates recurring patterns,
preferences, and facts into long-term memory records. This is the
actual consolidation logic that AutoDreamController's gate chain
triggers when all conditions are met.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# Consolidation prompt template
_CONSOLIDATION_PROMPT = """\
You are a memory consolidation agent. Review the following recent memory records \
and extract durable insights — recurring patterns, user preferences, project facts, \
and behavioral rules that should persist across sessions.

## Recent Memories

{memories_text}

## Instructions

1. Identify RECURRING patterns across multiple memories (not one-off facts).
2. Merge overlapping or contradictory memories into unified records.
3. Extract user preferences and behavioral rules.
4. Discard ephemeral task details that won't be useful in future sessions.

Return a JSON array of consolidated memory objects:
```json
[
  {{
    "title": "Short descriptive title",
    "content": "Consolidated insight or rule",
    "kind": "preference" | "fact" | "rule" | "pattern",
    "tags": ["tag1", "tag2"],
    "source_ids": ["id1", "id2"]
  }}
]
```

Only return memories worth keeping long-term. If nothing is worth consolidating, return `[]`.
"""

# Maximum memories to review per consolidation
MAX_MEMORIES_TO_REVIEW = 50

# Maximum turns in a single consolidation
MAX_CONSOLIDATION_TURNS = 30


class MemoryStoreProtocol(Protocol):
    """Minimal store interface needed by consolidator."""

    def list_recent(
        self, agent_id: str, user_id: str, limit: int,
    ) -> list[Any]: ...

    def save(self, record: Any) -> str: ...

    def update(self, record: Any) -> None: ...

    def delete(self, memory_id: str) -> None: ...


class ModelAdapterProtocol(Protocol):
    """Minimal adapter interface needed by consolidator."""

    async def generate(
        self, messages: list[dict], **kwargs: Any,
    ) -> Any: ...


@dataclass(frozen=True)
class ConsolidationResult:
    """Outcome of a consolidation run."""

    merged: int = 0        # Existing memories updated
    created: int = 0       # New consolidated memories created
    skipped: int = 0       # Candidates skipped (duplicates)
    source_ids: list[str] = field(default_factory=list)  # Source memory IDs reviewed
    errors: list[str] = field(default_factory=list)


class MemoryConsolidator:
    """LLM-based memory consolidation for AutoDream.

    Loads recent memories from the store, asks an LLM to find patterns
    and consolidate, then writes the results back.
    """

    def __init__(
        self,
        store: MemoryStoreProtocol,
        adapter: ModelAdapterProtocol,
        *,
        agent_id: str = "system",
        user_id: str = "default",
        max_memories: int = MAX_MEMORIES_TO_REVIEW,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._agent_id = agent_id
        self._user_id = user_id
        self._max_memories = max_memories

    async def consolidate(self) -> ConsolidationResult:
        """Run one consolidation cycle.

        Called by AutoDreamController when all gates pass.
        """
        # 1. Load recent memories
        try:
            recent = self._store.list_recent(
                self._agent_id, self._user_id, self._max_memories,
            )
        except Exception as e:
            logger.warning("consolidation.load_failed", error=str(e))
            return ConsolidationResult(errors=[f"Load failed: {e}"])

        if not recent:
            logger.debug("consolidation.no_memories")
            return ConsolidationResult()

        # 2. Build consolidation prompt
        source_ids = []
        memories_lines = []
        for mem in recent:
            mid = getattr(mem, "memory_id", str(id(mem)))
            title = getattr(mem, "title", "")
            content = getattr(mem, "content", str(mem))
            kind = getattr(mem, "kind", "unknown")
            source_ids.append(mid)
            memories_lines.append(
                f"- [{mid}] ({kind}) {title}: {content}"
            )

        memories_text = "\n".join(memories_lines)
        prompt = _CONSOLIDATION_PROMPT.format(memories_text=memories_text)

        # 3. Call LLM (adapters use complete() not generate())
        try:
            from agent_framework.models.message import Message
            messages = [Message(role="user", content=prompt)]
            response = await self._adapter.complete(messages, temperature=0.3)
            response_text = self._extract_text(response)
        except Exception as e:
            logger.warning("consolidation.llm_failed", error=str(e))
            return ConsolidationResult(
                source_ids=source_ids,
                errors=[f"LLM call failed: {e}"],
            )

        # 4. Parse response
        candidates = self._parse_candidates(response_text)
        if not candidates:
            logger.info("consolidation.nothing_to_consolidate")
            return ConsolidationResult(source_ids=source_ids)

        # 5. Write consolidated memories
        created = 0
        errors = []
        for candidate in candidates:
            try:
                self._save_consolidated(candidate)
                created += 1
            except Exception as e:
                errors.append(f"Save failed for '{candidate.get('title', '?')}': {e}")

        result = ConsolidationResult(
            created=created,
            source_ids=source_ids,
            errors=errors,
        )

        logger.info(
            "consolidation.complete",
            created=created,
            reviewed=len(source_ids),
            errors=len(errors),
        )
        return result

    def _extract_text(self, response: Any) -> str:
        """Extract text content from model response."""
        if isinstance(response, str):
            return response
        if hasattr(response, "content"):
            return str(response.content)
        if hasattr(response, "text"):
            return str(response.text)
        if isinstance(response, dict):
            return response.get("content", response.get("text", str(response)))
        return str(response)

    def _parse_candidates(self, text: str) -> list[dict]:
        """Parse JSON array of consolidated memories from LLM response."""
        # Extract JSON from markdown code block if present
        if "```json" in text:
            start = text.index("```json") + 7
            end = text.index("```", start)
            text = text[start:end].strip()
        elif "```" in text:
            start = text.index("```") + 3
            end = text.index("```", start)
            text = text[start:end].strip()

        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [c for c in parsed if isinstance(c, dict) and c.get("title")]
            return []
        except (json.JSONDecodeError, ValueError):
            logger.debug("consolidation.parse_failed", text_preview=text[:200])
            return []

    def _save_consolidated(self, candidate: dict) -> None:
        """Save a single consolidated memory to the store."""
        from agent_framework.models.memory import MemoryKind, MemoryRecord

        # Map LLM kind strings to MemoryKind enum
        kind_str = candidate.get("kind", "").upper()
        kind_map = {
            "PREFERENCE": MemoryKind.USER_PREFERENCE,
            "USER_PREFERENCE": MemoryKind.USER_PREFERENCE,
            "FACT": MemoryKind.PROJECT_CONTEXT,
            "PROJECT_CONTEXT": MemoryKind.PROJECT_CONTEXT,
            "RULE": MemoryKind.USER_CONSTRAINT,
            "USER_CONSTRAINT": MemoryKind.USER_CONSTRAINT,
            "PATTERN": MemoryKind.CUSTOM,
            "CUSTOM": MemoryKind.CUSTOM,
        }
        kind = kind_map.get(kind_str, MemoryKind.CUSTOM)

        record = MemoryRecord(
            agent_id=self._agent_id,
            user_id=self._user_id,
            kind=kind,
            title=candidate["title"],
            content=candidate.get("content", ""),
            tags=candidate.get("tags", []),
            source="consolidation",
            is_pinned=False,
            is_active=True,
        )
        self._store.save(record)

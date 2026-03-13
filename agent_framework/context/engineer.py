from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.models.context import ContextStats
from agent_framework.models.message import Message

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentConfig, AgentState, Skill
    from agent_framework.models.memory import MemoryRecord
    from agent_framework.models.session import SessionSnapshot, SessionState


class ContextEngineer:
    """Orchestrates context preparation for LLM calls.

    Coordinates ContextSourceProvider, ContextBuilder, and ContextCompressor.

    Read-only consumer contract:
    - ContextEngineer MUST NOT modify any input state:
      * SessionState — no appending, clearing, or removing messages
      * MemoryRecord list — no activating, deactivating, or deleting
      * AgentState — no modifying iteration_count, status, or any field
    - Compression results only affect the message list sent to the LLM
      in THIS call. They NEVER write back to SessionState or modify
      iteration_history.
    - The only mutable state is internal bookkeeping (_skill_prompt,
      _last_stats) which does not leak to callers.
    - Violation of this contract turns the context layer from a pure
      transformer into a read-write layer, causing state corruption
      in concurrent or re-entrant scenarios.
    """

    def __init__(
        self,
        source_provider: ContextSourceProvider | None = None,
        builder: ContextBuilder | None = None,
        compressor: ContextCompressor | None = None,
    ) -> None:
        self._source = source_provider or ContextSourceProvider()
        self._builder = builder or ContextBuilder()
        self._compressor = compressor or ContextCompressor()
        self._skill_prompt: str | None = None
        self._last_stats = ContextStats()

    def prepare_context_for_llm(
        self,
        agent_state: AgentState,
        context_materials: dict,
    ) -> list[Message]:
        """Build the final context message list.

        context_materials should contain:
        - agent_config: AgentConfig
        - session_state: SessionState
        - memories: list[MemoryRecord]
        - task: str
        - active_skill: Skill | None (optional)
        - runtime_info: dict | None (optional)
        """
        agent_config: AgentConfig = context_materials["agent_config"]
        session_state: SessionState = context_materials["session_state"]
        memories: list[MemoryRecord] = context_materials.get("memories", [])
        task: str = context_materials.get("task", agent_state.task)
        active_skill: Skill | None = context_materials.get("active_skill")
        runtime_info: dict | None = context_materials.get("runtime_info")

        # Collect from each source
        system_core = self._source.collect_system_core(agent_config, runtime_info)
        skill_addon = self._skill_prompt or self._source.collect_skill_addon(active_skill)
        memory_block = self._source.collect_saved_memory_block(memories)
        session_groups = self._source.collect_session_groups(session_state)
        current_input = self._source.collect_current_input(task)

        # Apply compressor before final build when session history is large.
        budget = getattr(self._builder, "_max_tokens", 8192) - getattr(
            self._builder, "_reserve_for_output", 1024
        )

        # Calculate per-slot token estimates
        system_msg = Message(role="system", content=system_core)
        system_tokens = self._builder.calculate_tokens([system_msg])

        memory_tokens = 0
        if memory_block:
            memory_tokens = self._builder.calculate_tokens(
                [Message(role="system", content=memory_block)]
            )

        session_tokens = 0
        for g in session_groups:
            session_tokens += self._builder.calculate_tokens(g.messages)

        input_tokens = self._builder.calculate_tokens([current_input])
        fixed_tokens = system_tokens + memory_tokens + input_tokens
        target_session_tokens = max(0, budget - fixed_tokens)
        session_groups = self._compressor.compress_groups(
            session_groups, target_tokens=target_session_tokens
        )

        # Build context
        messages = self._builder.build_context(
            system_core=system_core,
            skill_addon=skill_addon,
            memory_block=memory_block,
            session_groups=session_groups,
            current_input=current_input,
        )

        total_tokens = self._builder.calculate_tokens(messages)

        # Count trimmed groups
        actual_session_msgs = [m for m in messages if m.role not in ("system",) and m != current_input]
        original_session_msgs_count = sum(len(g.messages) for g in session_groups)
        groups_trimmed = max(0, original_session_msgs_count - len(actual_session_msgs))

        self._last_stats = ContextStats(
            system_tokens=system_tokens,
            memory_tokens=memory_tokens,
            session_tokens=session_tokens,
            input_tokens=input_tokens,
            total_tokens=total_tokens,
            groups_trimmed=groups_trimmed,
        )

        return messages

    def set_skill_context(self, skill_prompt: str | None) -> None:
        self._skill_prompt = skill_prompt

    def build_spawn_seed(
        self,
        session_messages: list[Message],
        query: str,
        token_budget: int,
    ) -> list[Message]:
        """Build a seed context for a child agent (doc 8.8). Delegates to ContextBuilder."""
        return self._builder.build_spawn_seed(session_messages, query, token_budget)

    def report_context_stats(self) -> ContextStats:
        return self._last_stats

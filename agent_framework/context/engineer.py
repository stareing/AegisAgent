from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.prefix_manager import PromptPrefixManager
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.hooks.dispatcher import HookDispatchService
from agent_framework.hooks.payloads import (context_post_build_payload,
                                            context_pre_build_payload)
from agent_framework.models.context import ContextStats
from agent_framework.models.hook import HookPoint
from agent_framework.models.message import Message

if TYPE_CHECKING:
    from agent_framework.models.agent import (AgentConfig, AgentState,
                                              ContextPolicy, Skill)
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
        hook_executor: Any = None,
    ) -> None:
        self._source = source_provider or ContextSourceProvider()
        self._builder = builder or ContextBuilder()
        self._compressor = compressor or ContextCompressor()
        self._prefix_mgr = PromptPrefixManager()
        self._skill_prompt: str | None = None
        self._allow_compression = True
        self._force_include_memory = False
        self._last_stats = ContextStats()
        self._hook_executor = hook_executor
        self._hook_dispatcher: HookDispatchService | None = (
            HookDispatchService(hook_executor) if hook_executor is not None else None
        )

    async def prepare_context_for_llm(
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

        # CONTEXT_PRE_BUILD hook — supports MODIFY for extra_instructions, compression_preference
        _hook_extra_instructions: str | None = None
        if self._hook_dispatcher is not None:
            try:
                outcome = await self._hook_dispatcher.fire(
                    HookPoint.CONTEXT_PRE_BUILD,
                    run_id=agent_state.run_id,
                    payload=context_pre_build_payload(task, len(memories), len(session_state.messages)),
                )
                if "extra_instructions" in outcome.modifications:
                    _hook_extra_instructions = str(outcome.modifications["extra_instructions"])
                if "compression_preference" in outcome.modifications:
                    pref = outcome.modifications["compression_preference"]
                    if pref == "disable":
                        self._allow_compression = False
            except Exception:
                pass  # Context hooks are advisory

        # Collect from each source
        tool_entries: list = context_materials.get("tool_entries", [])
        system_core = self._source.collect_system_core(agent_config, runtime_info, tool_entries)
        skill_addon = self._skill_prompt or self._source.collect_skill_addon(active_skill)

        # Inject skill catalog so LLM knows which skills are available
        skill_descriptions: list = context_materials.get("skill_descriptions", [])
        skill_catalog = self._source.collect_skill_catalog(skill_descriptions)
        if skill_catalog and not skill_addon:
            skill_addon = skill_catalog
        elif skill_catalog and skill_addon:
            skill_addon = f"{skill_addon}\n\n{skill_catalog}"

        # --- Frozen Prefix (§14.8) ---
        # system_core + skill_addon form the prefix (identity-stable).
        # If inputs haven't changed, reuse the cached prefix.
        prefix = self._prefix_mgr.get_or_create(
            system_core, skill_addon,
            token_counter=self._builder.calculate_tokens,
        )
        prefix_reused = (prefix.prefix_epoch > 1 or
                         (self._prefix_mgr.current_prefix is not None
                          and not self._prefix_mgr.should_rotate(system_core, skill_addon)))
        system_tokens = prefix.token_estimate

        # --- Dynamic state (per-iteration, outside frozen prefix) ---
        # current_iteration, spawned_subagents, todo_summary change each
        # iteration. These are NOT part of system_core so the prefix hash
        # stays stable and the cache is actually reused.
        dynamic_state = self._source.collect_dynamic_state(runtime_info)

        # --- Session context (Gemini-style environment awareness) ---
        # Appended to system message so LLM knows date, platform, git branch.
        # Only injected on first iteration to avoid token waste.
        session_context = None
        if agent_state.iteration_count == 0:
            session_context = self._source.collect_session_context(runtime_info)

        # --- Suffix: memories + session ---
        # Task is already in SessionState as the first user message (written by
        # RunCoordinator at run start).  Do NOT re-inject as current_input —
        # that would place the user message AFTER tool results, making the LLM
        # think it is a new request and causing repeated tool calls.
        memory_block = self._source.collect_saved_memory_block(memories)
        session_groups = self._source.collect_session_groups(session_state)

        memory_tokens = 0
        if memory_block:
            memory_tokens = self._builder.calculate_tokens(
                [Message(role="system", content=memory_block)]
            )

        session_tokens = 0
        for g in session_groups:
            session_tokens += self._builder.calculate_tokens(g.messages)
        original_session_msgs_count = sum(len(g.messages) for g in session_groups)

        # Budget = total - output_reserve
        budget = getattr(self._builder, "_max_tokens", 8192) - getattr(
            self._builder, "_reserve_for_output", 1024
        )

        # Compression: only when allowed by policy and in STATELESS mode.
        # In STATEFUL mode, compression would break delta indexing.
        is_stateful = context_materials.get("stateful_session", False)
        if not is_stateful and self._allow_compression:
            fixed_tokens = system_tokens + memory_tokens
            target_session_tokens = max(0, budget - fixed_tokens)
            model_adapter = context_materials.get("model_adapter")
            session_groups = await self._compressor.compress_groups_async(
                session_groups,
                target_tokens=target_session_tokens,
                model_adapter=model_adapter,
            )

        # ══════════════════════════════════════════════════════════
        # KV CACHE-OPTIMAL MESSAGE ORDERING
        #
        # LLM KV cache is prefix-matched: if the first K tokens are
        # identical to the previous call, those K tokens are reused.
        # Any change at position P invalidates ALL cache from P onward.
        #
        # Optimal ordering for maximum cache reuse:
        #
        #   messages[0]     = system (frozen prefix)     — IMMUTABLE
        #   messages[1..N]  = session history             — STABLE (old turns never change)
        #   messages[N+1]   = context injection           — CHANGES per iteration
        #
        # Between iteration i and i+1, only NEW messages are appended
        # to session history. The frozen prefix + all prior history
        # tokens form an identical prefix → KV cache fully reused.
        #
        # The injection message (dynamic state, memories, hooks) goes
        # LAST so its per-iteration changes never invalidate prior cache.
        #
        # Frozen prefix rotation triggers (exhaustive):
        #   ✓ tools/MCP added/removed     → system_core hash changes
        #   ✓ skill activated/deactivated  → skill_addon hash changes
        #   ✓ approval_mode changed        → system_core hash changes
        #   ✗ iteration progress           — does NOT rotate
        #   ✗ memory changes               — does NOT rotate
        #   ✗ todo state                   — does NOT rotate
        # ══════════════════════════════════════════════════════════

        # 1. Frozen prefix — IMMUTABLE, only rotates on hash change
        messages = list(prefix.messages)

        # 2. Session history — STABLE (prior turns are append-only, never mutated)
        for group in session_groups:
            messages.extend(group.messages)

        # 3. Context injection — LAST position, changes per iteration
        #    Placed after session history so it never invalidates KV cache
        #    for the prefix + prior turns.
        injection_parts: list[str] = []

        if session_context:
            injection_parts.append(session_context)

        if memory_block:
            injection_parts.append(memory_block)

        if dynamic_state:
            injection_parts.append(dynamic_state)

        if _hook_extra_instructions:
            injection_parts.append(
                f"<hook-instructions>{_hook_extra_instructions}</hook-instructions>"
            )

        if injection_parts:
            messages.append(Message(
                role="user",
                content="<context-update>\n"
                + "\n\n".join(injection_parts)
                + "\n</context-update>",
            ))

        total_tokens = self._builder.calculate_tokens(messages)

        # Count session messages (excluding frozen prefix and trailing injection)
        injection_count = 1 if injection_parts else 0
        actual_session_msgs_count = len(messages) - len(prefix.messages) - injection_count
        groups_trimmed = max(0, original_session_msgs_count - actual_session_msgs_count)

        self._last_stats = ContextStats(
            system_tokens=system_tokens,
            memory_tokens=memory_tokens,
            session_tokens=session_tokens,
            input_tokens=0,
            total_tokens=total_tokens,
            groups_trimmed=groups_trimmed,
            prefix_reused=prefix_reused,
        )

        if self._hook_dispatcher is not None:
            await self._hook_dispatcher.fire_advisory(
                HookPoint.CONTEXT_POST_BUILD,
                run_id=agent_state.run_id,
                payload=context_post_build_payload(
                    len(messages), total_tokens, groups_trimmed, prefix_reused,
                ),
            )

        return messages

    def apply_context_policy(self, policy: ContextPolicy) -> None:
        """Apply run-scoped context policy. Called by RunCoordinator.

        This is the sole entry point for ContextPolicy consumption.
        RunCoordinator passes the policy; only ContextEngineer interprets its fields.
        """
        self._allow_compression = policy.allow_compression
        self._force_include_memory = policy.force_include_saved_memory

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

    def reset_compressor(self) -> None:
        """Reset compressor state (frozen summary). Called at run start."""
        if hasattr(self._compressor, "reset"):
            self._compressor.reset()

    def report_context_stats(self) -> ContextStats:
        return self._last_stats

    def set_tools_schema_tokens(self, tools_schema: list[dict]) -> int:
        """Attach tool-schema token estimate to latest context stats."""
        token_est = len(json.dumps(tools_schema or [], default=str)) // 4
        self._last_stats = self._last_stats.model_copy(
            update={"tools_schema_tokens": token_est}
        )
        return token_est

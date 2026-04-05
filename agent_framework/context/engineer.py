from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.post_compact_restorer import PostCompactRestorer
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
        tool_registry: Any = None,
    ) -> None:
        self._source = source_provider or ContextSourceProvider()
        self._builder = builder or ContextBuilder()
        self._compressor = compressor or ContextCompressor()
        self._prefix_mgr = PromptPrefixManager()
        self._post_compact_restorer = PostCompactRestorer()
        self._skill_prompt: str | None = None
        self._allow_compression = True
        self._force_include_memory = False
        self._auto_compact_threshold: float = 0.7
        self._last_stats = ContextStats()
        self._hook_executor = hook_executor
        # v4.2: Registry for template-based tool result summarization during SNIP
        self._tool_registry = tool_registry
        # v4.3: Time-based tool result clearing (lightest compaction stage)
        from agent_framework.context.time_based_clearing import TimeBasedClearing
        self._time_based_clearing = TimeBasedClearing()
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
        skill_addon_override = self._skill_prompt or self._source.collect_skill_addon(active_skill)

        # Inject skill catalog so LLM knows which skills are available
        skill_descriptions: list = context_materials.get("skill_descriptions", [])
        skill_catalog = self._source.collect_skill_catalog(skill_descriptions)
        if skill_catalog and not skill_addon_override:
            skill_addon_override = skill_catalog
        elif skill_catalog and skill_addon_override:
            skill_addon_override = f"{skill_addon_override}\n\n{skill_catalog}"

        # --- Frozen Prefix (§14.8) ---
        # v4.3: Use section-based prefix (per-section caching, volatile separation).
        # build_prompt_sections produces independently-cached sections; only
        # cached section hash changes trigger prefix rotation.
        section_registry = self._source.build_prompt_sections(
            agent_config, runtime_info, tool_entries, active_skill,
        )
        # Skill catalog is an extra section if present
        if skill_addon_override:
            from agent_framework.context.prompt_sections import prompt_section
            section_registry.register(prompt_section(
                "skill_catalog", lambda t=skill_addon_override: t,
            ))
        prefix = self._prefix_mgr.get_or_create_from_sections(
            section_registry,
            token_counter=self._builder.calculate_tokens,
        )
        prefix_reused = (prefix.prefix_epoch > 1 or
                         (self._prefix_mgr.current_prefix is not None
                          and prefix.prefix_hash == self._prefix_mgr.current_prefix.prefix_hash))
        system_tokens = prefix.token_estimate

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
            # v4.3: Strip images before compression to prevent token bloat
            from agent_framework.context.compressor import ContextCompressor as _CC
            session_groups = _CC.strip_images(session_groups)
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

        # --- Auto-compaction trigger ---
        # If total tokens exceed auto_compact_threshold of budget and compression
        # is allowed, run an additional SNIP pass on tool messages then restore
        # recently-accessed files and active skill context.
        if (
            not is_stateful
            and self._allow_compression
            and budget > 0
            and total_tokens > self._auto_compact_threshold * budget
        ):
            import logging as _logging
            _auto_logger = _logging.getLogger(__name__)
            _auto_logger.info(
                "context.auto_compact_triggered tokens=%d threshold=%.0f%% budget=%d",
                total_tokens,
                self._auto_compact_threshold * 100,
                budget,
            )

            target_session_tokens = max(0, budget - system_tokens - memory_tokens)
            compact_groups = self._source.collect_session_groups(session_state)

            # Staged auto-compaction (lightest → heaviest):
            # Stage 1: Time-based clearing — free expired tool results
            all_flat: list[Message] = []
            for g in compact_groups:
                all_flat.extend(g.messages)
            if self._time_based_clearing.should_trigger(all_flat):
                compact_groups = self._time_based_clearing.clear_old_tool_results(compact_groups)
                _auto_logger.info("context.auto_compact.stage1_time_clearing")
                stage1_tokens = sum(
                    self._builder.calculate_tokens(g.messages) for g in compact_groups
                )
                if stage1_tokens <= target_session_tokens:
                    _auto_logger.info("context.auto_compact.stage1_sufficient tokens=%d", stage1_tokens)
                    compact_groups = compact_groups  # Already within budget
                else:
                    # Stage 2: SNIP — template-based tool result summarization
                    from agent_framework.context.compressor import ContextCompressor
                    snip_compressor = ContextCompressor(
                        token_counter=self._builder.calculate_tokens,
                        strategy="SNIP",
                        tool_registry=self._tool_registry,
                    )
                    compact_groups = await snip_compressor.compress_groups_async(
                        compact_groups, target_tokens=target_session_tokens,
                    )
            else:
                # No time gap → go directly to SNIP
                from agent_framework.context.compressor import ContextCompressor
                snip_compressor = ContextCompressor(
                    token_counter=self._builder.calculate_tokens,
                    strategy="SNIP",
                    tool_registry=self._tool_registry,
                )
                compact_groups = await snip_compressor.compress_groups_async(
                    compact_groups, target_tokens=target_session_tokens,
                )

            # Rebuild messages with compacted groups
            messages = list(prefix.messages)
            for group in compact_groups:
                messages.extend(group.messages)
            if injection_parts:
                messages.append(Message(
                    role="user",
                    content="<context-update>\n"
                    + "\n\n".join(injection_parts)
                    + "\n</context-update>",
                ))
            # Post-compact restoration
            recently_accessed_files: list[str] = context_materials.get(
                "recently_accessed_files", []
            )
            messages = self._post_compact_restorer.restore(
                messages,
                recently_accessed_files=recently_accessed_files,
                active_skill=active_skill,
            )
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

        # v4.3: Mark cache breakpoints for provider-side KV cache optimization.
        # Two breakpoints: (1) system prefix, (2) last session message before injection.
        # Adapters that support caching (Anthropic) will inject cache_control markers;
        # others (OpenAI/Doubao) silently ignore the field.
        messages = self._mark_cache_breakpoints(messages, bool(injection_parts))

        return messages

    @staticmethod
    def _mark_cache_breakpoints(
        messages: list[Message], has_injection: bool
    ) -> list[Message]:
        """Mark cache breakpoints on messages for provider-side KV cache reuse.

        Two breakpoints (Claude Code pattern):
        1. System prefix (messages[0]) — frozen across turns, highest cache value
        2. Last session message before injection — stable prefix grows each turn

        Returns a new list with cache_control set on marked messages.
        """
        if not messages:
            return messages

        result = list(messages)
        cache_hint = {"type": "ephemeral"}

        # Breakpoint 1: System prefix (always slot 0)
        if result[0].role == "system":
            result[0] = result[0].model_copy(update={"cache_control": cache_hint})

        # Breakpoint 2: Last message before injection (or absolute last)
        # Injection is the trailing <context-update> user message
        last_session_idx = len(result) - (1 if has_injection else 0) - 1
        if last_session_idx > 0 and last_session_idx < len(result):
            result[last_session_idx] = result[last_session_idx].model_copy(
                update={"cache_control": cache_hint}
            )

        return result

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

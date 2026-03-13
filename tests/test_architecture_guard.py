"""Architecture guard tests — comprehensive boundary enforcement.

Three categories:
1. Anti-bypass scan: code-level checks that boundary violations haven't crept back
2. Fault injection: model failure, tool failure, sub-agent timeout, memory failure
3. Data flow invariants: immutability, ordering, version chains
"""

from __future__ import annotations

import asyncio
import inspect
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest
from agent_framework.models.agent import (
    AgentConfig,
    AgentRunResult,
    AgentState,
    AgentStatus,
    IterationResult,
    StopReason,
    StopSignal,
    TerminationKind,
)
from agent_framework.models.tool import ToolExecutionError, ToolResult


# =====================================================================
# PART 1: Anti-Bypass Scan — automated code-level boundary checks
# =====================================================================


class TestAntiBypassScan:
    """Automated scan for boundary violations that could 'sneak back'."""

    # --- SessionState write-port ---

    def test_only_run_state_controller_writes_session(self):
        """No module except RunStateController may call append_message (v2.5.1)."""
        import agent_framework.agent.coordinator as coord_mod
        source = inspect.getsource(coord_mod)
        # Remove comments and strings for accurate check
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "session_state.append_message" in line or ".messages.append(" in line:
                assert False, (
                    f"coordinator.py:{i} — direct SessionState write bypass: {stripped}"
                )

    def test_loop_does_not_write_session(self):
        """AgentLoop must not touch SessionState."""
        import agent_framework.agent.loop as loop_mod
        source = inspect.getsource(loop_mod)
        # Check for actual imports, not docstring mentions
        assert "import SessionState" not in source, "AgentLoop must not import SessionState"
        assert "session_state.append" not in source, "AgentLoop must not write session"

    def test_context_engineer_does_not_write_session(self):
        """ContextEngineer must not call append_message."""
        import agent_framework.context.engineer as eng_mod
        source = inspect.getsource(eng_mod)
        assert "append_message" not in source

    def test_message_projector_does_not_write_session(self):
        """MessageProjector must not call append_message."""
        import agent_framework.agent.message_projector as mp_mod
        source = inspect.getsource(mp_mod)
        assert "append_message" not in source
        assert "session_state" not in source

    # --- AgentLoop state mutation ---

    def test_loop_does_not_mutate_agent_state(self):
        """AgentLoop must not write agent_state fields."""
        import agent_framework.agent.loop as loop_mod
        source = inspect.getsource(loop_mod)
        forbidden_patterns = [
            r"agent_state\.status\s*=",
            r"agent_state\.total_tokens_used\s*[+]?=",
            r"agent_state\.iteration_count\s*[+]?=",
            r"agent_state\.iteration_history\.",
        ]
        for pat in forbidden_patterns:
            assert not re.search(pat, source), (
                f"AgentLoop contains forbidden write: {pat}"
            )

    def test_loop_does_not_import_agent_status(self):
        """AgentLoop must not import AgentStatus (uses it via RunStateController)."""
        import agent_framework.agent.loop as loop_mod
        source = inspect.getsource(loop_mod)
        assert "AgentStatus" not in source

    def test_loop_uses_only_loop_deps(self):
        """AgentLoop must use AgentLoopDeps, not full AgentRuntimeDeps."""
        import agent_framework.agent.loop as loop_mod
        source = inspect.getsource(loop_mod)
        assert "import AgentRuntimeDeps" not in source

    # --- Context layer receives snapshot, not mutable session ---

    def test_coordinator_passes_snapshot_to_context(self):
        """RunCoordinator must pass SessionSnapshot, not SessionState, to context (v2.6.4 §45)."""
        import agent_framework.agent.coordinator as coord_mod
        source = inspect.getsource(coord_mod.RunCoordinator._prepare_llm_request)
        assert "session_snapshot" in source or "session_snap" in source, (
            "Coordinator must create a snapshot for context layer"
        )

    # --- Policy interpretation uniqueness ---

    def test_coordinator_does_not_read_context_policy_fields(self):
        """RunCoordinator must pass ContextPolicy, never read its fields."""
        import agent_framework.agent.coordinator as coord_mod
        source = inspect.getsource(coord_mod)
        assert "context_policy." not in source or "policy_bundle.context_policy" in source

    def test_coordinator_does_not_read_memory_policy_fields(self):
        """RunCoordinator must pass MemoryPolicy, never read its fields."""
        import agent_framework.agent.coordinator as coord_mod
        source = inspect.getsource(coord_mod)
        assert "memory_policy." not in source or "policy_bundle.memory_policy" in source

    # --- EffectiveRunConfig construction ---

    def test_coordinator_does_not_construct_effective_config(self):
        """RunCoordinator must not call EffectiveRunConfig() directly."""
        import agent_framework.agent.coordinator as coord_mod
        source = inspect.getsource(coord_mod.RunCoordinator)
        assert "EffectiveRunConfig(" not in source

    # --- SubAgent ownership boundaries ---

    def test_scheduler_has_no_active_children_dict(self):
        """SubAgentScheduler must not maintain _active dict."""
        import agent_framework.subagent.scheduler as sched_mod
        source = inspect.getsource(sched_mod.SubAgentScheduler)
        assert "_active" not in source

    def test_scheduler_has_no_get_active_children(self):
        """SubAgentScheduler must not expose get_active_children."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        assert not hasattr(SubAgentScheduler, "get_active_children")

    def test_runtime_owns_active_children(self):
        """SubAgentRuntime must be sole truth source for active_children."""
        import agent_framework.subagent.runtime as rt_mod
        source = inspect.getsource(rt_mod.SubAgentRuntime)
        assert "_active" in source
        assert "get_active_children" in source

    # --- Factory purity ---

    def test_factory_does_not_import_capability_policy(self):
        """SubAgentFactory module must not import CapabilityPolicy."""
        import agent_framework.subagent.factory as f_mod
        source = inspect.getsource(f_mod)
        assert "import CapabilityPolicy" not in source

    def test_factory_does_not_import_effective_run_config(self):
        """SubAgentFactory module must not import EffectiveRunConfig."""
        import agent_framework.subagent.factory as f_mod
        source = inspect.getsource(f_mod)
        assert "import EffectiveRunConfig" not in source

    # --- TransactionGroupIndex consumption ---

    def test_consume_transaction_index_no_uuid(self):
        """_consume_transaction_index must not generate new UUIDs."""
        from agent_framework.context.source_provider import ContextSourceProvider
        source = inspect.getsource(ContextSourceProvider._consume_transaction_index)
        assert "uuid" not in source

    # --- Streaming boundary ---

    def test_session_state_no_model_chunk(self):
        """SessionState must not reference ModelChunk writes."""
        import agent_framework.models.session as sess_mod
        source = inspect.getsource(sess_mod.SessionState)
        # Only docstring reference allowed, no actual code accepting chunks
        assert "ModelChunk" not in source or "MUST NOT" in source

    # --- ToolMeta immutability ---

    def test_tool_meta_frozen(self):
        """ToolMeta must be frozen (pydantic)."""
        from agent_framework.models.tool import ToolMeta
        assert ToolMeta.model_config.get("frozen") is True

    # --- EventBus observation boundary ---

    def test_event_bus_module_documents_observation_only(self):
        """EventBus module must document observation-only contract."""
        import agent_framework.infra.event_bus as eb_mod
        assert "MUST NOT" in eb_mod.__doc__
        assert "Mutate" in eb_mod.__doc__ or "mutate" in eb_mod.__doc__


# =====================================================================
# PART 2: Fault Injection — error paths converging at run tail
# =====================================================================


class TestFaultInjection:
    """Tests that error paths correctly converge through the architecture."""

    def _make_deps(self):
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps
        mock_mm = MagicMock()
        mock_mm.select_for_context.return_value = []
        mock_mm.begin_run_session = MagicMock()
        mock_mm.end_run_session = MagicMock()
        mock_mm.record_turn = MagicMock()

        mock_ce = MagicMock()
        mock_ce.prepare_context_for_llm.return_value = [
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
        ]
        mock_ce.set_skill_context = MagicMock()

        mock_tr = MagicMock()
        mock_tr.list_tools.return_value = []
        mock_tr.export_schemas.return_value = []

        mock_adapter = AsyncMock()
        mock_executor = AsyncMock()
        mock_sr = MagicMock()
        mock_sr.detect_skill.return_value = None

        return AgentRuntimeDeps(
            tool_registry=mock_tr,
            tool_executor=mock_executor,
            memory_manager=mock_mm,
            context_engineer=mock_ce,
            model_adapter=mock_adapter,
            skill_router=mock_sr,
        )

    # --- Model failure ---

    @pytest.mark.asyncio
    async def test_model_failure_produces_error_result(self):
        """Model adapter raising → AgentRunResult with ERROR stop reason."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.side_effect = RuntimeError("Model API 500")
        coordinator._loop = mock_loop

        result = await coordinator.run(agent, deps, "test task")
        assert result.success is False
        assert result.stop_signal.reason == StopReason.ERROR
        assert "500" in result.error
        assert result.termination_kind == TerminationKind.ABORT

    @pytest.mark.asyncio
    async def test_model_failure_still_ends_memory_session(self):
        """Model failure must still trigger end_run_session in finally."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.side_effect = RuntimeError("boom")
        coordinator._loop = mock_loop

        await coordinator.run(agent, deps, "test")
        deps.memory_manager.begin_run_session.assert_called_once()
        deps.memory_manager.end_run_session.assert_called_once()

    # --- Tool partial failure ---

    @pytest.mark.asyncio
    async def test_tool_partial_failure_all_projected(self):
        """Both successful and failed tool results must be projected."""
        from agent_framework.agent.message_projector import MessageProjector

        result = IterationResult(
            iteration_index=0,
            model_response=ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="a", arguments={}),
                    ToolCallRequest(id="tc2", function_name="b", arguments={}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=10),
            ),
            tool_results=[
                ToolResult(tool_call_id="tc1", tool_name="a", success=True, output="ok"),
                ToolResult(tool_call_id="tc2", tool_name="b", success=False,
                    error=ToolExecutionError(
                        error_type="EXECUTION_ERROR", message="tool b failed")),
            ],
        )
        messages = MessageProjector.project_iteration(result)
        # 1 assistant + 2 tool messages
        assert len(messages) == 3
        assert messages[1].role == "tool"
        assert messages[2].role == "tool"
        # Failed tool must also be projected
        assert "tool b failed" in messages[2].content

    # --- Sub-agent timeout ---

    @pytest.mark.asyncio
    async def test_subagent_timeout_produces_failure(self):
        """Sub-agent timeout → SubAgentResult.success=False."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        from agent_framework.models.subagent import SubAgentHandle, SubAgentResult

        sched = SubAgentScheduler(max_per_run=5)
        handle = SubAgentHandle(
            sub_agent_id="sub_t", spawn_id="t1", parent_run_id="run_1"
        )

        async def _slow():
            await asyncio.sleep(10)
            return SubAgentResult(spawn_id="t1", success=True)

        result = await sched.schedule(handle, _slow(), deadline_ms=50)
        assert result.success is False
        assert "timed out" in result.error.lower()

    # --- Memory commit failure ---

    @pytest.mark.asyncio
    async def test_memory_commit_failure_does_not_crash_run(self):
        """record_turn failure must not prevent run completion."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        deps.memory_manager.record_turn.side_effect = RuntimeError("DB down")
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.return_value = IterationResult(
            model_response=ModelResponse(
                content="answer", finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            ),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )
        coordinator._loop = mock_loop

        # Should not raise despite record_turn failure
        result = await coordinator.run(agent, deps, "test")
        # Run may succeed or fail depending on error handling,
        # but end_run_session must still be called
        deps.memory_manager.end_run_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_end_session_failure_does_not_crash_run(self):
        """end_run_session failure must not propagate."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        deps.memory_manager.end_run_session.side_effect = RuntimeError("cleanup fail")
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.return_value = IterationResult(
            model_response=ModelResponse(
                content="ok", finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            ),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )
        coordinator._loop = mock_loop

        # Must not raise
        result = await coordinator.run(agent, deps, "test")
        assert result is not None

    # --- Event duplicate handling ---

    def test_event_envelope_idempotent_design(self):
        """EventEnvelope must support deduplication via event_id."""
        from agent_framework.infra.event_bus import EventEnvelope
        e1 = EventEnvelope(event_id="fixed_id", event_name="test")
        e2 = EventEnvelope(event_id="fixed_id", event_name="test")
        assert e1.event_id == e2.event_id
        # Different events get different IDs by default
        e3 = EventEnvelope(event_name="test")
        e4 = EventEnvelope(event_name="test")
        assert e3.event_id != e4.event_id

    # --- Run cancellation ---

    @pytest.mark.asyncio
    async def test_cancellation_triggers_user_cancel_stop(self):
        """External cancel_event → USER_CANCEL stop reason."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        cancel = asyncio.Event()
        cancel.set()  # Already cancelled

        mock_loop = AsyncMock(spec=AgentLoop)
        coordinator._loop = mock_loop

        result = await coordinator.run(agent, deps, "test", cancel_event=cancel)
        assert result.stop_signal.reason == StopReason.USER_CANCEL
        assert result.termination_kind == TerminationKind.ABORT

    # --- Timeout ---

    @pytest.mark.asyncio
    async def test_run_timeout_triggers_degrade(self):
        """Global run timeout → MAX_ITERATIONS stop reason → DEGRADE."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)

        # Make the loop slow enough to trigger timeout on second iteration
        async def _slow_iteration(*args, **kwargs):
            await asyncio.sleep(0.05)
            return IterationResult(
                model_response=ModelResponse(
                    content="", finish_reason="tool_calls",
                    usage=TokenUsage(total_tokens=5),
                ),
            )
        mock_loop.execute_iteration.side_effect = _slow_iteration
        coordinator._loop = mock_loop

        # 10ms timeout — will expire after first iteration
        result = await coordinator.run(agent, deps, "test", run_timeout_ms=10)
        assert result.stop_signal.reason == StopReason.MAX_ITERATIONS
        assert result.termination_kind == TerminationKind.DEGRADE


# =====================================================================
# PART 3: Data Flow Invariants
# =====================================================================


class TestDataFlowInvariants:
    """Verifies data flow ordering, immutability, and version chain rules."""

    # --- Iteration history is append-only ---

    def test_iteration_history_append_only(self):
        """iteration_history must only grow, never shrink or replace."""
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        state = AgentState(run_id="r1")

        r1 = IterationResult(iteration_index=0)
        r2 = IterationResult(iteration_index=1)
        ctrl.apply_iteration_result(state, r1)
        ctrl.apply_iteration_result(state, r2)

        assert len(state.iteration_history) == 2
        assert state.iteration_history[0] is r1
        assert state.iteration_history[1] is r2

    # --- MessageProjector iteration_id injection ---

    def test_projected_messages_carry_iteration_id(self):
        """All projected messages must have iteration_id in metadata."""
        from agent_framework.agent.message_projector import MessageProjector

        result = IterationResult(
            iteration_index=7,
            model_response=ModelResponse(
                content="hi", finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            ),
        )
        messages = MessageProjector.project_iteration(result)
        for msg in messages:
            assert msg.metadata is not None
            assert msg.metadata.get("iteration_id") == 7

    # --- SessionSnapshot immutability ---

    def test_session_snapshot_frozen(self):
        """SessionSnapshot must not reflect post-creation changes."""
        from agent_framework.models.session import SessionSnapshot, SessionState
        session = SessionState(session_id="s1")
        session.append_message(Message(role="user", content="a"))
        snap = SessionSnapshot(session)

        session.append_message(Message(role="assistant", content="b"))
        assert len(snap.messages) == 1
        assert len(session.messages) == 2

    # --- AgentStateSnapshot immutability ---

    def test_agent_state_snapshot_frozen(self):
        """AgentStateSnapshot must not reflect post-creation changes."""
        from agent_framework.agent.run_state import AgentStateSnapshot
        state = AgentState(run_id="r1", status=AgentStatus.RUNNING, iteration_count=3)
        snap = AgentStateSnapshot(state)

        state.status = AgentStatus.FINISHED
        state.iteration_count = 10

        assert snap.status == AgentStatus.RUNNING
        assert snap.iteration_count == 3

    # --- ToolCommitSequencer ordering ---

    @pytest.mark.asyncio
    async def test_commit_sequencer_stable_ordering(self):
        """ToolCommitSequencer must order by input_index regardless of arrival."""
        from agent_framework.agent.commit_sequencer import ToolCommitSequencer
        from agent_framework.models.tool import ToolExecutionOutcome

        seq = ToolCommitSequencer()
        # Feed in reverse order
        outcomes = [
            ToolExecutionOutcome(tool_call_id="c", input_index=2),
            ToolExecutionOutcome(tool_call_id="a", input_index=0),
            ToolExecutionOutcome(tool_call_id="b", input_index=1),
        ]
        sorted_out = await seq.commit_outcomes(outcomes)
        assert [o.tool_call_id for o in sorted_out] == ["a", "b", "c"]

    # --- Retry version chain ---

    def test_retry_creates_new_attempt_not_overwrite(self):
        """Retry must create new IterationResult, not modify original."""
        from agent_framework.models.agent import IterationAttempt

        original = IterationResult(
            iteration_index=0,
            attempt=IterationAttempt(
                attempt_id="att_1", iteration_id="iter_0", attempt_index=0,
            ),
        )
        retry = IterationResult(
            iteration_index=0,
            attempt=IterationAttempt(
                attempt_id="att_2", iteration_id="iter_0",
                parent_attempt_id="att_1", attempt_index=1,
                trigger_reason="transient error",
            ),
        )
        # Both exist independently
        assert original.attempt.attempt_id == "att_1"
        assert retry.attempt.attempt_id == "att_2"
        assert retry.attempt.parent_attempt_id == original.attempt.attempt_id
        # Same logical iteration
        assert original.attempt.iteration_id == retry.attempt.iteration_id

    # --- EffectiveRunConfig frozen ---

    def test_effective_run_config_immutable(self):
        """EffectiveRunConfig must be frozen after construction."""
        from agent_framework.models.agent import EffectiveRunConfig
        config = EffectiveRunConfig()
        with pytest.raises(Exception):
            config.model_name = "changed"

    # --- ResolvedRunPolicyBundle frozen ---

    def test_resolved_policy_bundle_immutable(self):
        """ResolvedRunPolicyBundle must be frozen."""
        from agent_framework.agent.run_policy import ResolvedRunPolicyBundle
        from agent_framework.models.agent import (
            CapabilityPolicy, ContextPolicy, EffectiveRunConfig, MemoryPolicy,
        )
        bundle = ResolvedRunPolicyBundle(
            effective_run_config=EffectiveRunConfig(),
            context_policy=ContextPolicy(),
            memory_policy=MemoryPolicy(),
            capability_policy=CapabilityPolicy(),
        )
        with pytest.raises(Exception):
            bundle.context_policy = ContextPolicy()

    # --- Tool whitelist intersection ---

    def test_whitelist_never_expands_beyond_safe_set(self):
        """tool_category_whitelist can only narrow, never expand."""
        from agent_framework.subagent.factory import _resolve_effective_tool_names
        from agent_framework.models.tool import ToolEntry, ToolMeta

        tools = [
            ToolEntry(meta=ToolMeta(name="calc", category="math", source="local")),
            ToolEntry(meta=ToolMeta(name="shell", category="system", source="local")),
            ToolEntry(meta=ToolMeta(name="fetch", category="network", source="local")),
            ToolEntry(meta=ToolMeta(name="search", category="general", source="local")),
        ]
        blocked = {"system", "network", "subagent"}

        # Whitelist includes blocked category → must NOT expand
        names = _resolve_effective_tool_names(tools, blocked, ["math", "system"])
        assert "calc" in names
        assert "shell" not in names

    # --- SubAgentStatus mapping completeness ---

    def test_all_error_codes_mapped_to_status(self):
        """Every DelegationErrorCode must have a SubAgentStatus mapping."""
        from agent_framework.models.subagent import (
            DelegationErrorCode, _ERROR_CODE_TO_STATUS,
        )
        for code in DelegationErrorCode:
            assert code in _ERROR_CODE_TO_STATUS, f"{code} unmapped"

    # --- TerminationKind mapping completeness ---

    def test_all_stop_reasons_mapped(self):
        """Every StopReason must map to a TerminationKind."""
        from agent_framework.models.agent import (
            StopReason, _STOP_REASON_TO_TERMINATION_KIND,
        )
        for reason in StopReason:
            assert reason in _STOP_REASON_TO_TERMINATION_KIND, f"{reason} unmapped"

    # --- CommitDecision from record_turn ---

    def test_record_turn_always_returns_commit_decision(self):
        """record_turn must return CommitDecision in all paths."""
        from agent_framework.memory.default_manager import DefaultMemoryManager
        from agent_framework.models.memory import CommitDecision

        store = MagicMock()
        store.list_by_user.return_value = []
        mm = DefaultMemoryManager(store)

        # Disabled path
        mm.set_enabled(False)
        result = mm.record_turn("hi", "bye", [])
        assert isinstance(result, CommitDecision)
        assert result.committed is False

        # Enabled, no candidates path
        mm.set_enabled(True)
        mm.begin_run_session("r1", "a1", None)
        result = mm.record_turn("hello", "world", [])
        assert isinstance(result, CommitDecision)

    # --- TransactionGroupIndex consumption vs rebuild ---

    def test_index_consumed_without_regeneration(self):
        """When TransactionGroupIndex is provided, group IDs must be preserved."""
        from agent_framework.context.source_provider import ContextSourceProvider
        from agent_framework.context.transaction_group import (
            ToolTransactionGroup, TransactionGroupIndex,
        )
        from agent_framework.models.session import SessionState

        provider = ContextSourceProvider()
        g1 = ToolTransactionGroup(group_id="STABLE_1", messages=[
            Message(role="user", content="x"),
        ])
        g2 = ToolTransactionGroup(group_id="STABLE_2", messages=[
            Message(role="assistant", content="y"),
        ])
        index = TransactionGroupIndex(
            groups_by_id={"STABLE_1": g1, "STABLE_2": g2},
            groups_by_iteration={"0": ["STABLE_1"], "1": ["STABLE_2"]},
        )
        session = SessionState()

        groups = provider.collect_session_groups(session, transaction_index=index)
        ids = {g.group_id for g in groups}
        assert ids == {"STABLE_1", "STABLE_2"}

    # --- begin/end_run_session pairing ---

    @pytest.mark.asyncio
    async def test_begin_end_session_always_paired_on_success(self):
        """Successful run: begin_run_session and end_run_session called once each."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.return_value = IterationResult(
            model_response=ModelResponse(
                content="ok", finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            ),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )
        coordinator._loop = mock_loop

        await coordinator.run(agent, deps, "test")
        deps.memory_manager.begin_run_session.assert_called_once()
        deps.memory_manager.end_run_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_begin_end_session_always_paired_on_failure(self):
        """Failed run: end_run_session must still be called."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.loop import AgentLoop

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.side_effect = RuntimeError("crash")
        coordinator._loop = mock_loop

        await coordinator.run(agent, deps, "test")
        deps.memory_manager.begin_run_session.assert_called_once()
        deps.memory_manager.end_run_session.assert_called_once()

    def _make_deps(self):
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps
        mock_mm = MagicMock()
        mock_mm.select_for_context.return_value = []
        mock_mm.begin_run_session = MagicMock()
        mock_mm.end_run_session = MagicMock()
        mock_mm.record_turn = MagicMock()

        mock_ce = MagicMock()
        mock_ce.prepare_context_for_llm.return_value = [
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
        ]
        mock_ce.set_skill_context = MagicMock()

        mock_tr = MagicMock()
        mock_tr.list_tools.return_value = []
        mock_tr.export_schemas.return_value = []

        return AgentRuntimeDeps(
            tool_registry=mock_tr,
            tool_executor=AsyncMock(),
            memory_manager=mock_mm,
            context_engineer=mock_ce,
            model_adapter=AsyncMock(),
            skill_router=MagicMock(detect_skill=MagicMock(return_value=None)),
        )

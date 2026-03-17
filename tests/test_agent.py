"""Strict unit tests for agent layer.

Covers:
- BaseAgent hooks and strategy methods
- DefaultAgent construction
- ReActAgent (final answer pattern, step limits, error policy)
- SkillRouter (detection, activation, deactivation)
- AgentLoop (iteration, stop conditions, tool dispatch)
- RunCoordinator (full run lifecycle)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.agent.base_agent import BaseAgent
from agent_framework.agent.default_agent import DefaultAgent
from agent_framework.agent.react_agent import ReActAgent
from agent_framework.agent.skill_router import SkillRouter
from agent_framework.agent.loop import AgentLoop, AgentLoopDeps
from agent_framework.agent.coordinator import RunCoordinator
from agent_framework.models.agent import (
    AgentConfig,
    AgentRunResult,
    AgentState,
    AgentStatus,
    CapabilityPolicy,
    ContextPolicy,
    EffectiveRunConfig,
    ErrorStrategy,
    IterationResult,
    MemoryPolicy,
    Skill,
    SpawnDecision,
    StopDecision,
    StopReason,
    StopSignal,
    TerminationKind,
    ToolCallDecision,
)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest
from agent_framework.models.stream import StreamEvent, StreamEventType
from agent_framework.models.tool import ToolExecutionMeta, ToolResult


# =====================================================================
# BaseAgent
# =====================================================================


class TestBaseAgent:
    def test_init(self):
        config = AgentConfig(agent_id="test", model_name="gpt-4")
        agent = BaseAgent(config)
        assert agent.agent_id == "test"
        assert agent.agent_config.model_name == "gpt-4"

    @pytest.mark.asyncio
    async def test_on_before_run_noop(self):
        agent = BaseAgent(AgentConfig())
        state = AgentState(run_id="r1")
        await agent.on_before_run("task", state)  # should not raise

    @pytest.mark.asyncio
    async def test_on_iteration_started_noop(self):
        agent = BaseAgent(AgentConfig())
        state = AgentState(run_id="r1")
        await agent.on_iteration_started(0, state)

    @pytest.mark.asyncio
    async def test_on_tool_call_requested_allows_by_default(self):
        agent = BaseAgent(AgentConfig())
        req = ToolCallRequest(id="tc1", function_name="test", arguments={})
        decision = await agent.on_tool_call_requested(req)
        assert isinstance(decision, ToolCallDecision)
        assert decision.allowed is True

    @pytest.mark.asyncio
    async def test_on_tool_call_completed_noop(self):
        agent = BaseAgent(AgentConfig())
        result = ToolResult(tool_call_id="tc1", tool_name="test", success=True)
        await agent.on_tool_call_completed(result)

    @pytest.mark.asyncio
    async def test_on_spawn_requested_respects_config(self):
        agent = BaseAgent(AgentConfig(allow_spawn_children=False))
        from agent_framework.models.subagent import SubAgentSpec
        spec = SubAgentSpec(task_input="sub task")
        decision = await agent.on_spawn_requested(spec)
        assert isinstance(decision, SpawnDecision)
        assert decision.allowed is False

        agent2 = BaseAgent(AgentConfig(allow_spawn_children=True))
        decision2 = await agent2.on_spawn_requested(spec)
        assert decision2.allowed is True

    @pytest.mark.asyncio
    async def test_on_final_answer_noop(self):
        agent = BaseAgent(AgentConfig())
        state = AgentState(run_id="r1")
        await agent.on_final_answer("answer", state)

    def test_should_stop_on_stop_signal(self):
        agent = BaseAgent(AgentConfig(max_iterations=10))
        state = AgentState(iteration_count=0)
        result = IterationResult(stop_signal=StopSignal(reason=StopReason.LLM_STOP))
        decision = agent.should_stop(result, state)
        assert isinstance(decision, StopDecision)
        assert decision.should_stop is True

    def test_should_stop_on_max_iterations(self):
        agent = BaseAgent(AgentConfig(max_iterations=5))
        state = AgentState(iteration_count=5)
        result = IterationResult()
        decision = agent.should_stop(result, state)
        assert decision.should_stop is True
        assert decision.stop_signal is not None
        assert decision.stop_signal.reason == StopReason.MAX_ITERATIONS

    def test_should_not_stop_normally(self):
        agent = BaseAgent(AgentConfig(max_iterations=10))
        state = AgentState(iteration_count=2)
        result = IterationResult()
        decision = agent.should_stop(result, state)
        assert decision.should_stop is False

    def test_get_error_policy_returns_none(self):
        agent = BaseAgent(AgentConfig())
        state = AgentState()
        assert agent.get_error_policy(RuntimeError(), state) is None

    def test_get_context_policy(self):
        agent = BaseAgent(AgentConfig())
        policy = agent.get_context_policy(AgentState())
        assert isinstance(policy, ContextPolicy)

    def test_get_memory_policy(self):
        agent = BaseAgent(AgentConfig())
        policy = agent.get_memory_policy(AgentState())
        assert isinstance(policy, MemoryPolicy)

    def test_get_capability_policy_reflects_spawn_config(self):
        agent = BaseAgent(AgentConfig(allow_spawn_children=True))
        policy = agent.get_capability_policy()
        assert policy.allow_spawn is True

        agent2 = BaseAgent(AgentConfig(allow_spawn_children=False))
        policy2 = agent2.get_capability_policy()
        assert policy2.allow_spawn is False


# =====================================================================
# DefaultAgent
# =====================================================================


class TestDefaultAgent:
    def test_default_construction(self):
        agent = DefaultAgent()
        assert agent.agent_id == "default"
        assert agent.agent_config.model_name == "gpt-3.5-turbo"
        assert agent.agent_config.max_iterations == 20

    def test_custom_construction(self):
        agent = DefaultAgent(
            agent_id="custom",
            system_prompt="custom prompt",
            model_name="gpt-4",
            max_iterations=10,
            temperature=0.5,
            allow_spawn_children=True,
        )
        assert agent.agent_id == "custom"
        assert agent.agent_config.system_prompt == "custom prompt"
        assert agent.agent_config.model_name == "gpt-4"
        assert agent.agent_config.max_iterations == 10
        assert agent.agent_config.temperature == 0.5
        assert agent.agent_config.allow_spawn_children is True


# =====================================================================
# OrchestratorAgent
# =====================================================================


class TestOrchestratorAgent:

    def test_default_construction(self):
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        agent = OrchestratorAgent()
        assert agent.agent_id == "orchestrator"
        assert agent.agent_config.allow_spawn_children is True
        assert agent.agent_config.max_iterations == 0

    def test_orchestrator_prompt_contains_delegation(self):
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        agent = OrchestratorAgent()
        prompt = agent.agent_config.system_prompt
        assert "spawn_agent" in prompt
        assert "delegate" in prompt.lower()
        assert "decision policy" in prompt.lower()

    @pytest.mark.asyncio
    async def test_spawn_approved_by_default(self):
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        from agent_framework.models.subagent import SubAgentSpec
        agent = OrchestratorAgent()
        decision = await agent.on_spawn_requested(SubAgentSpec(task_input="test"))
        assert decision.allowed is True

    def test_custom_model_name(self):
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        agent = OrchestratorAgent(model_name="gpt-4", temperature=0.3)
        assert agent.agent_config.model_name == "gpt-4"
        assert agent.agent_config.temperature == 0.3

    def test_custom_prompt_overrides_default(self):
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        agent = OrchestratorAgent(system_prompt="Custom orchestrator")
        assert agent.agent_config.system_prompt == "Custom orchestrator"

    def test_prompt_allows_parallel_tool_calls(self):
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        agent = OrchestratorAgent()
        prompt = agent.agent_config.system_prompt
        assert "parallel" in prompt.lower() or "multiple tools" in prompt.lower()
        # Must NOT say "Call ONE tool at a time" (that's for workers)
        assert "Call ONE tool at a time" not in prompt

    def test_prompt_has_resource_awareness(self):
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        agent = OrchestratorAgent()
        prompt = agent.agent_config.system_prompt
        assert "agent-capabilities" in prompt
        assert "max_iterations" in prompt or "iteration" in prompt.lower()

    def test_coordinator_cancels_subagents_on_exit(self):
        """Coordinator must cancel active sub-agents in finally block."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        assert "cancel_all" in source

    def test_orchestrator_hard_exit_guard(self):
        """Orchestrator must force stop after N post-spawn iterations when enabled."""
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        # Enable the guard via instance parameter (not global constant)
        agent = OrchestratorAgent(max_post_spawn_iterations=3)
        state = AgentState(
            run_id="r1", spawn_count=2, iteration_count=10,
            last_spawn_iteration_index=5,  # O(1) lookup
        )
        for i in range(10):
            state.iteration_history.append(IterationResult(iteration_index=i))
        result = IterationResult(iteration_index=10)
        decision = agent.should_stop(result, state)
        assert decision.should_stop is True
        assert "synthesis budget" in decision.reason.lower() or "spawn" in decision.reason.lower()

    def test_orchestrator_hard_exit_guard_disabled(self):
        """When max_post_spawn_iterations <= 0, guard does not trigger."""
        from agent_framework.agent.orchestrator_agent import OrchestratorAgent
        agent = OrchestratorAgent()  # default: 0 = unlimited
        state = AgentState(
            run_id="r1", spawn_count=2, iteration_count=10,
            last_spawn_iteration_index=5,
        )
        for i in range(10):
            state.iteration_history.append(IterationResult(iteration_index=i))
        result = IterationResult(iteration_index=10)
        decision = agent.should_stop(result, state)
        assert decision.should_stop is False

    def test_spawn_count_updated_on_successful_spawn(self):
        """apply_iteration_result must increment spawn_count on spawn_agent success."""
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        state = AgentState(run_id="r1")
        result = IterationResult(
            iteration_index=0,
            tool_results=[
                ToolResult(tool_call_id="tc1", tool_name="spawn_agent", success=True, output="done"),
                ToolResult(tool_call_id="tc2", tool_name="read_file", success=True, output="data"),
            ],
        )
        ctrl.apply_iteration_result(state, result)
        assert state.spawn_count == 1

    def test_last_spawn_iteration_index_updated(self):
        """apply_iteration_result must update last_spawn_iteration_index on spawn."""
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        state = AgentState(run_id="r1")
        assert state.last_spawn_iteration_index == -1

        # First iteration: no spawn
        ctrl.apply_iteration_result(state, IterationResult(
            iteration_index=0,
            tool_results=[ToolResult(tool_call_id="tc1", tool_name="read_file", success=True, output="data")],
        ))
        assert state.last_spawn_iteration_index == -1

        # Second iteration: spawn
        ctrl.apply_iteration_result(state, IterationResult(
            iteration_index=1,
            tool_results=[ToolResult(tool_call_id="tc2", tool_name="spawn_agent", success=True, output="done")],
        ))
        assert state.last_spawn_iteration_index == 1
        assert state.spawn_count == 1

    def test_spawn_count_not_updated_on_failure(self):
        """apply_iteration_result must not count failed spawns."""
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        state = AgentState(run_id="r1")
        result = IterationResult(
            iteration_index=0,
            tool_results=[
                ToolResult(tool_call_id="tc1", tool_name="spawn_agent", success=False, output="quota exceeded"),
            ],
        )
        ctrl.apply_iteration_result(state, result)
        assert state.spawn_count == 0

    def test_parent_run_id_uses_run_id_not_agent_id(self):
        """ToolExecutor must use _current_run_id as primary parent_run_id."""
        import inspect
        from agent_framework.tools.executor import ToolExecutor
        source = inspect.getsource(ToolExecutor._subagent_spawn)
        # Primary assignment uses _current_run_id
        assert "parent_run_id = self._current_run_id" in source
        # set_current_run_id must exist
        assert hasattr(ToolExecutor, "set_current_run_id")


# =====================================================================
# ReActAgent
# =====================================================================


class TestReActAgent:
    def test_default_construction(self):
        agent = ReActAgent()
        assert agent.agent_id == "react"
        assert "ReAct" in agent.agent_config.system_prompt

    def test_custom_system_prompt_appended(self):
        agent = ReActAgent(system_prompt="Use math tools only")
        assert "Use math tools only" in agent.agent_config.system_prompt
        assert "ReAct" in agent.agent_config.system_prompt

    def test_extract_final_answer(self):
        assert ReActAgent.extract_final_answer("Final Answer: 42") == "42"
        assert ReActAgent.extract_final_answer("Final Answer：Yes") == "Yes"
        assert ReActAgent.extract_final_answer("no answer here") is None

    def test_extract_final_answer_multiline(self):
        text = "Some thought\nFinal Answer: The result is\n42"
        result = ReActAgent.extract_final_answer(text)
        assert result is not None
        assert "42" in result

    def test_should_stop_on_final_answer(self):
        agent = ReActAgent()
        state = AgentState(iteration_count=0)
        response = ModelResponse(content="Final Answer: done", finish_reason="stop")
        result = IterationResult(model_response=response)
        decision = agent.should_stop(result, state)
        assert decision.should_stop is True
        assert decision.stop_signal is not None
        assert decision.stop_signal.reason == StopReason.CUSTOM

    def test_should_stop_on_max_react_steps(self):
        agent = ReActAgent(max_react_steps=3)
        state = AgentState(iteration_count=3)
        result = IterationResult(model_response=ModelResponse(content="thinking...", finish_reason="stop"))
        decision = agent.should_stop(result, state)
        assert decision.should_stop is True

    def test_should_not_stop_mid_reasoning(self):
        agent = ReActAgent(max_react_steps=10)
        state = AgentState(iteration_count=2)
        response = ModelResponse(content="Let me think about this", finish_reason="tool_calls")
        result = IterationResult(model_response=response)
        decision = agent.should_stop(result, state)
        assert decision.should_stop is False

    def test_error_policy_retry_when_possible(self):
        agent = ReActAgent(max_iterations=10)
        state = AgentState(iteration_count=5)
        assert agent.get_error_policy(RuntimeError(), state) == ErrorStrategy.RETRY

    def test_error_policy_abort_at_last_iteration(self):
        agent = ReActAgent(max_iterations=10)
        state = AgentState(iteration_count=9)
        assert agent.get_error_policy(RuntimeError(), state) == ErrorStrategy.ABORT


# =====================================================================
# SkillRouter
# =====================================================================


class TestSkillRouter:
    def test_register_and_detect(self):
        router = SkillRouter()
        skill = Skill(skill_id="math", name="Math", trigger_keywords=["calculate", "compute"])
        router.register_skill(skill)

        detected = router.detect_skill("Please calculate 2+2")
        assert detected is not None
        assert detected.skill_id == "math"

    def test_detect_no_match(self):
        router = SkillRouter()
        skill = Skill(skill_id="math", name="Math", trigger_keywords=["calculate"])
        router.register_skill(skill)
        assert router.detect_skill("Hello world") is None

    def test_detect_case_insensitive(self):
        router = SkillRouter()
        skill = Skill(skill_id="math", name="Math", trigger_keywords=["Calculate"])
        router.register_skill(skill)
        assert router.detect_skill("CALCULATE this") is not None

    def test_get_skill_by_id(self):
        """SkillRouter is now a pure catalog — no activation state."""
        router = SkillRouter()
        skill = Skill(skill_id="math", name="Math", system_prompt_addon="Use math")
        router.register_skill(skill)
        assert router.get_skill("math") == skill
        assert router.get_skill("nonexistent") is None

    def test_list_skills(self):
        router = SkillRouter()
        router.register_skill(Skill(skill_id="a", name="A"))
        router.register_skill(Skill(skill_id="b", name="B"))
        assert len(router.list_skills()) == 2

    def test_multiple_skills_first_match_wins(self):
        router = SkillRouter()
        router.register_skill(Skill(skill_id="s1", trigger_keywords=["hello"]))
        router.register_skill(Skill(skill_id="s2", trigger_keywords=["hello"]))
        detected = router.detect_skill("hello")
        assert detected is not None


# =====================================================================
# AgentLoop
# =====================================================================


class TestAgentLoop:
    def _make_agent(self, max_iterations=10):
        return DefaultAgent(max_iterations=max_iterations)

    def _make_state(self, iteration_count=0):
        return AgentState(run_id="r1", task="test", iteration_count=iteration_count)

    def _make_config(self):
        return EffectiveRunConfig(model_name="test-model")

    def _make_llm_request(self):
        return LLMRequest(
            messages=[Message(role="user", content="hi")],
            tools_schema=[],
        )

    @pytest.mark.asyncio
    async def test_iteration_llm_stop(self):
        loop = AgentLoop()
        agent = self._make_agent()
        state = self._make_state()
        config = self._make_config()
        request = self._make_llm_request()

        mock_adapter = AsyncMock()
        mock_adapter.complete.return_value = ModelResponse(
            content="Hello!", finish_reason="stop", usage=TokenUsage(total_tokens=10)
        )
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)

        result = await loop.execute_iteration(
            agent, AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor),
            state, request, config
        )
        assert result.stop_signal is not None
        assert result.stop_signal.reason == StopReason.LLM_STOP
        assert result.model_response.content == "Hello!"

    @pytest.mark.asyncio
    async def test_iteration_with_tool_calls(self):
        loop = AgentLoop()
        agent = self._make_agent()
        state = self._make_state()
        config = self._make_config()
        request = self._make_llm_request()

        tc = ToolCallRequest(id="tc1", function_name="search", arguments={"q": "test"})
        mock_adapter = AsyncMock()
        mock_adapter.complete.return_value = ModelResponse(
            content="", finish_reason="tool_calls",
            tool_calls=[tc],
            usage=TokenUsage(total_tokens=20),
        )
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
        mock_executor.batch_execute.return_value = [
            (ToolResult(tool_call_id="tc1", tool_name="search", success=True, output="found"),
             ToolExecutionMeta(execution_time_ms=10, source="local"))
        ]

        result = await loop.execute_iteration(
            agent, AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor),
            state, request, config
        )
        assert result.stop_signal is None
        assert len(result.tool_results) == 1
        assert result.tool_results[0].success is True

    @pytest.mark.asyncio
    async def test_iteration_trims_parallel_tool_calls_when_disabled(self):
        loop = AgentLoop()
        agent = self._make_agent()
        state = self._make_state()
        config = EffectiveRunConfig(
            model_name="test-model",
            allow_parallel_tool_calls=False,
        )
        request = self._make_llm_request()

        tc1 = ToolCallRequest(id="tc1", function_name="search", arguments={"q": "a"})
        tc2 = ToolCallRequest(id="tc2", function_name="search", arguments={"q": "b"})

        mock_adapter = AsyncMock()
        mock_adapter.complete.return_value = ModelResponse(
            content="",
            finish_reason="tool_calls",
            tool_calls=[tc1, tc2],
            usage=TokenUsage(total_tokens=20),
        )
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
        mock_executor.batch_execute.return_value = [
            (
                ToolResult(
                    tool_call_id="tc1",
                    tool_name="search",
                    success=True,
                    output="found-a",
                ),
                ToolExecutionMeta(execution_time_ms=10, source="local"),
            ),
        ]

        result = await loop.execute_iteration(
            agent,
            AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor),
            state,
            request,
            config,
        )

        assert len(result.model_response.tool_calls) == 1
        assert result.model_response.tool_calls[0].id == "tc1"
        assert len(result.tool_results) == 1
        dispatched_calls = mock_executor.batch_execute.call_args.args[0]
        assert len(dispatched_calls) == 1
        assert dispatched_calls[0].id == "tc1"

    @pytest.mark.asyncio
    async def test_iteration_llm_error_abort(self):
        loop = AgentLoop()
        agent = self._make_agent()
        state = self._make_state()
        config = self._make_config()
        request = self._make_llm_request()

        mock_adapter = AsyncMock()
        mock_adapter.complete.side_effect = RuntimeError("API down")
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)

        result = await loop.execute_iteration(
            agent, AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor),
            state, request, config
        )
        assert result.stop_signal is not None
        assert result.stop_signal.reason == StopReason.ERROR
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_iteration_output_truncated(self):
        loop = AgentLoop()
        agent = self._make_agent()
        state = self._make_state()
        config = self._make_config()
        request = self._make_llm_request()

        mock_adapter = AsyncMock()
        mock_adapter.complete.return_value = ModelResponse(
            content="partial...", finish_reason="length",
            usage=TokenUsage(total_tokens=50),
        )
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)

        result = await loop.execute_iteration(
            agent, AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor),
            state, request, config
        )
        assert result.stop_signal.reason == StopReason.OUTPUT_TRUNCATED

    @pytest.mark.asyncio
    async def test_iteration_max_iterations_stop(self):
        loop = AgentLoop()
        agent = self._make_agent(max_iterations=3)
        state = self._make_state(iteration_count=2)
        config = self._make_config()
        request = self._make_llm_request()

        mock_adapter = AsyncMock()
        mock_adapter.complete.return_value = ModelResponse(
            content="", finish_reason="tool_calls",
            tool_calls=[ToolCallRequest(id="tc1", function_name="f", arguments={})],
            usage=TokenUsage(total_tokens=10),
        )
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)

        result = await loop.execute_iteration(
            agent, AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor),
            state, request, config
        )
        assert result.stop_signal is not None
        assert result.stop_signal.reason == StopReason.MAX_ITERATIONS

    @pytest.mark.asyncio
    async def test_tool_blocked_by_agent_hook(self):
        """Agent's on_tool_call_requested returns ToolCallDecision(allowed=False)."""
        loop = AgentLoop()
        agent = self._make_agent()

        # Override hook to block
        async def deny_tool(req):
            return ToolCallDecision(allowed=False, reason="test block")
        agent.on_tool_call_requested = deny_tool

        state = self._make_state()
        config = self._make_config()
        request = self._make_llm_request()

        tc = ToolCallRequest(id="tc1", function_name="blocked", arguments={})
        mock_adapter = AsyncMock()
        mock_adapter.complete.return_value = ModelResponse(
            content="", finish_reason="tool_calls",
            tool_calls=[tc],
            usage=TokenUsage(total_tokens=10),
        )
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
        mock_executor.is_tool_allowed = MagicMock(return_value=True)

        result = await loop.execute_iteration(
            agent, AgentLoopDeps(model_adapter=mock_adapter, tool_executor=mock_executor),
            state, request, config
        )
        # Blocked tool calls now return rejection feedback to the LLM
        assert len(result.tool_results) == 1
        assert result.tool_results[0].success is False
        assert "denied" in result.tool_results[0].output.lower()
        mock_executor.batch_execute.assert_not_called()


# =====================================================================
# RunCoordinator
# =====================================================================


class TestRunCoordinator:
    def _make_deps(self):
        """Create mock AgentRuntimeDeps."""
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps

        mock_mm = MagicMock()
        mock_mm.select_for_context.return_value = []
        mock_mm.begin_run_session = MagicMock()
        mock_mm.end_run_session = MagicMock()
        mock_mm.begin_session = MagicMock()
        mock_mm.end_session = MagicMock()
        mock_mm.record_turn = MagicMock()

        mock_ce = MagicMock()
        mock_ce.prepare_context_for_llm = AsyncMock(return_value=[
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
        ])
        mock_ce.set_skill_context = MagicMock()

        mock_tr = MagicMock()
        mock_tr.list_tools.return_value = []
        mock_tr.export_schemas.return_value = []

        mock_adapter = AsyncMock()
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
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

    def test_runtime_info_parallel_disabled_by_effective_config(self):
        deps = self._make_deps()
        deps.model_adapter.supports_parallel_tool_calls = MagicMock(return_value=True)

        info = RunCoordinator._collect_runtime_info(
            effective_config=EffectiveRunConfig(allow_parallel_tool_calls=False),
            deps=deps,
        )
        assert info["parallel_tool_calls"] == "false"

    def test_runtime_info_parallel_requires_adapter_support(self):
        deps = self._make_deps()
        deps.model_adapter.supports_parallel_tool_calls = MagicMock(return_value=False)

        info = RunCoordinator._collect_runtime_info(
            effective_config=EffectiveRunConfig(allow_parallel_tool_calls=True),
            deps=deps,
        )
        assert info["parallel_tool_calls"] == "false"

    def test_runtime_info_marks_code_investigation_tasks(self):
        info = RunCoordinator._collect_runtime_info(
            task="分析当前代码架构，不要偷懒看md那不是真实的",
        )
        assert info["investigation_mode"] == "codebase_analysis"
        assert "glob_files/grep_search" in info["investigation_expectation"]

    @pytest.mark.asyncio
    async def test_simple_run_success(self):
        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        # Mock loop to return stop after 1 iteration
        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.return_value = IterationResult(
            model_response=ModelResponse(content="The answer", finish_reason="stop", usage=TokenUsage(total_tokens=10)),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )
        coordinator._loop = mock_loop

        result = await coordinator.run(agent, deps, "What is 2+2?")
        assert result.success is True
        assert result.final_answer == "The answer"
        deps.memory_manager.begin_run_session.assert_called_once()
        deps.memory_manager.end_run_session.assert_called_once()
        deps.memory_manager.record_turn.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_passes_user_id_to_memory_session(self):
        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.return_value = IterationResult(
            model_response=ModelResponse(
                content="ok", finish_reason="stop", usage=TokenUsage(total_tokens=5)
            ),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )
        coordinator._loop = mock_loop

        result = await coordinator.run(agent, deps, "task", user_id="user_123")
        assert result.success is True
        deps.memory_manager.begin_run_session.assert_called_once()
        call_args = deps.memory_manager.begin_run_session.call_args.args
        assert call_args[1] == agent.agent_id
        assert call_args[2] == "user_123"

    @pytest.mark.asyncio
    async def test_run_with_error(self):
        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        # Make loop raise
        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.side_effect = RuntimeError("boom")
        coordinator._loop = mock_loop

        result = await coordinator.run(agent, deps, "task")
        assert result.success is False
        assert "boom" in result.error

    @pytest.mark.asyncio
    async def test_run_progressive_records_intermediate_responses(self):
        deps = self._make_deps()
        agent = DefaultAgent(progressive_tool_results=True)
        coordinator = RunCoordinator()

        first_iteration = IterationResult(
            model_response=ModelResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_1",
                        function_name="spawn_agent",
                        arguments={"task_input": "task 1", "wait": True},
                    ),
                    ToolCallRequest(
                        id="tc_2",
                        function_name="spawn_agent",
                        arguments={"task_input": "task 2", "wait": True},
                    ),
                ],
                finish_reason="tool_calls",
            ),
            tool_results=[
                ToolResult(tool_call_id="tc_1", tool_name="spawn_agent", success=True, output="result 1"),
                ToolResult(tool_call_id="tc_2", tool_name="spawn_agent", success=True, output="result 2"),
            ],
            tool_execution_meta=[
                ToolExecutionMeta(execution_time_ms=10, source="subagent"),
                ToolExecutionMeta(execution_time_ms=20, source="subagent"),
            ],
        )
        final_iteration = IterationResult(
            model_response=ModelResponse(
                content="final",
                finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            ),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.side_effect = [first_iteration, final_iteration]
        mock_loop._call_llm = AsyncMock(
            return_value=ModelResponse(content="mid response", finish_reason="stop")
        )
        coordinator._loop = mock_loop

        result = await coordinator.run(agent, deps, "task")

        assert result.success is True
        assert result.final_answer == "final"
        assert result.progressive_responses == ["mid response"]

    @pytest.mark.asyncio
    async def test_skill_detection(self):
        deps = self._make_deps()
        skill = Skill(skill_id="math", name="Math", trigger_keywords=["calculate"])
        deps.skill_router.detect_skill.return_value = skill

        agent = DefaultAgent()
        coordinator = RunCoordinator()

        mock_loop = AsyncMock(spec=AgentLoop)
        mock_loop.execute_iteration.return_value = IterationResult(
            model_response=ModelResponse(content="4", finish_reason="stop", usage=TokenUsage(total_tokens=5)),
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
        )
        coordinator._loop = mock_loop

        result = await coordinator.run(agent, deps, "calculate 2+2")
        assert result.success is True
        # Skill detection happened via router, activation via context engineer
        deps.skill_router.detect_skill.assert_called_once()
        deps.context_engineer.set_skill_context.assert_any_call(skill.system_prompt_addon)

    def test_build_effective_config_no_skill(self):
        from agent_framework.agent.run_policy import RunPolicyResolver
        agent = DefaultAgent(model_name="gpt-4", temperature=0.5)
        config = RunPolicyResolver.build_effective_config(agent, None)
        assert config.model_name == "gpt-4"
        assert config.temperature == 0.5

    def test_build_effective_config_with_skill_override(self):
        from agent_framework.agent.run_policy import RunPolicyResolver
        agent = DefaultAgent(model_name="gpt-3.5-turbo", temperature=0.7)
        skill = Skill(
            skill_id="code",
            model_override="gpt-4",
            temperature_override=0.2,
        )
        config = RunPolicyResolver.build_effective_config(agent, skill)
        assert config.model_name == "gpt-4"
        assert config.temperature == 0.2
        # Safety fields not overridden
        assert config.max_iterations == agent.agent_config.max_iterations

    def test_build_effective_config_carries_tool_parallel_policy(self):
        from agent_framework.agent.run_policy import RunPolicyResolver

        agent = DefaultAgent(
            allow_parallel_tool_calls=False,
            max_concurrent_tool_calls=1,
        )
        config = RunPolicyResolver.build_effective_config(agent, None)
        assert config.allow_parallel_tool_calls is False
        assert config.max_concurrent_tool_calls == 1

    def test_initialize_state(self):
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        state = ctrl.initialize_state("my task", "run_123")
        assert state.run_id == "run_123"
        assert state.task == "my task"
        assert state.status == AgentStatus.IDLE

    def test_finalize_run_success(self):
        agent = DefaultAgent()
        coordinator = RunCoordinator()
        state = AgentState(run_id="r1", total_tokens_used=100, iteration_count=3)
        stop = StopSignal(reason=StopReason.LLM_STOP)
        result = coordinator._finalize_run(agent, state, "final", stop)
        assert result.success is True
        assert result.final_answer == "final"
        assert result.usage.total_tokens == 100

    def test_finalize_run_error(self):
        agent = DefaultAgent()
        coordinator = RunCoordinator()
        state = AgentState(run_id="r1")
        stop = StopSignal(reason=StopReason.ERROR, message="err")
        result = coordinator._finalize_run(agent, state, None, stop)
        assert result.success is False


# ---------------------------------------------------------------------------
# v2.5.1 §18: Implementation red-line tests
# ---------------------------------------------------------------------------

class TestArchitecturalRedLines:
    """Tests that verify architectural boundaries are not violated."""

    def test_agent_loop_does_not_import_runtime_deps(self):
        """AgentLoop module must not import AgentRuntimeDeps."""
        import agent_framework.agent.loop as loop_mod
        import inspect
        # Check actual imports, not docstring mentions
        source = inspect.getsource(loop_mod)
        # Should not have "from ... import AgentRuntimeDeps" or "import AgentRuntimeDeps"
        assert "import AgentRuntimeDeps" not in source

    def test_agent_loop_uses_loop_deps(self):
        """AgentLoop.execute_iteration must accept AgentLoopDeps, not separate params."""
        import inspect
        sig = inspect.signature(AgentLoop.execute_iteration)
        param_names = list(sig.parameters.keys())
        assert "loop_deps" in param_names
        # Must NOT have model_adapter or tool_executor as direct params
        assert "model_adapter" not in param_names
        assert "tool_executor" not in param_names

    def test_skill_router_has_no_active_state(self):
        """SkillRouter must not hold per-run mutable state."""
        router = SkillRouter()
        assert not hasattr(router, "_active_skill")
        assert not hasattr(router, "activate_skill")
        assert not hasattr(router, "deactivate_current_skill")
        assert not hasattr(router, "get_active_skill")

    def test_run_state_controller_holds_active_skill(self):
        """RunStateController is the owner of active_skill state."""
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        assert ctrl.active_skill is None

        skill = Skill(skill_id="test", name="Test")
        state = ctrl.initialize_state("task", "run_1")
        ctrl.activate_skill(state, skill)
        assert ctrl.active_skill == skill
        assert state.active_skill_id == "test"

        ctrl.deactivate_skill(state)
        assert ctrl.active_skill is None
        assert state.active_skill_id is None

    def test_subagent_config_override_whitelist(self):
        """SubAgentConfigOverride only allows whitelisted fields."""
        from agent_framework.models.subagent import SubAgentConfigOverride
        override = SubAgentConfigOverride(model_name="gpt-4", temperature=0.5)
        assert override.model_name == "gpt-4"
        assert override.temperature == 0.5
        # Prohibited fields must not exist
        assert not hasattr(override, "max_iterations")
        assert not hasattr(override, "allow_spawn_children")

    def test_message_projector_does_not_write_session(self):
        """MessageProjector returns messages, does not write SessionState."""
        from agent_framework.agent.message_projector import MessageProjector
        projector = MessageProjector()
        result = IterationResult(
            model_response=ModelResponse(content="hi", finish_reason="stop"),
        )
        messages = projector.project_iteration(result)
        assert isinstance(messages, list)
        assert len(messages) == 1
        assert messages[0].role == "assistant"

    def test_authorization_decision_model(self):
        """AuthorizationDecision must carry reason and source_layer."""
        from agent_framework.models.tool import AuthorizationDecision
        denied = AuthorizationDecision(
            allowed=False,
            reason="category blocked",
            source_layer="CapabilityPolicy",
            normalized_tool_name="spawn_agent",
        )
        assert denied.allowed is False
        assert denied.reason == "category blocked"
        assert denied.source_layer == "CapabilityPolicy"

    def test_effective_run_config_frozen(self):
        """EffectiveRunConfig must be immutable after construction."""
        config = EffectiveRunConfig(model_name="gpt-4", temperature=0.3)
        with pytest.raises(Exception):
            config.temperature = 0.9


# ---------------------------------------------------------------------------
# v2.5.2 §29: Implementation red-line tests
# ---------------------------------------------------------------------------

class TestV252RedLines:
    """Tests that verify v2.5.2 architectural boundaries."""

    def test_decision_models_not_bare_bools(self):
        """Decision interfaces must return typed models, not bare bools (§19)."""
        agent = BaseAgent(AgentConfig())
        state = AgentState(iteration_count=0)
        result = IterationResult()

        # should_stop returns StopDecision
        decision = agent.should_stop(result, state)
        assert isinstance(decision, StopDecision)
        assert not isinstance(decision, bool)

    @pytest.mark.asyncio
    async def test_tool_call_decision_model(self):
        """on_tool_call_requested returns ToolCallDecision (§19)."""
        agent = BaseAgent(AgentConfig())
        req = ToolCallRequest(id="tc1", function_name="test", arguments={})
        decision = await agent.on_tool_call_requested(req)
        assert isinstance(decision, ToolCallDecision)
        assert hasattr(decision, "allowed")
        assert hasattr(decision, "reason")

    @pytest.mark.asyncio
    async def test_spawn_decision_model(self):
        """on_spawn_requested returns SpawnDecision (§19)."""
        agent = BaseAgent(AgentConfig(allow_spawn_children=False))
        from agent_framework.models.subagent import SubAgentSpec
        spec = SubAgentSpec(task_input="test")
        decision = await agent.on_spawn_requested(spec)
        assert isinstance(decision, SpawnDecision)
        assert decision.allowed is False
        assert decision.reason != ""  # must explain why

    def test_termination_kind_mapping(self):
        """StopSignal.termination_kind must classify correctly (§20)."""
        # Normal
        sig = StopSignal(reason=StopReason.LLM_STOP)
        assert sig.termination_kind == TerminationKind.NORMAL
        assert sig.is_normal is True

        sig2 = StopSignal(reason=StopReason.CUSTOM)
        assert sig2.termination_kind == TerminationKind.NORMAL

        # Abort
        sig3 = StopSignal(reason=StopReason.ERROR)
        assert sig3.termination_kind == TerminationKind.ABORT
        assert sig3.is_abort is True

        sig4 = StopSignal(reason=StopReason.USER_CANCEL)
        assert sig4.is_abort is True

        # Degrade
        sig5 = StopSignal(reason=StopReason.MAX_ITERATIONS)
        assert sig5.termination_kind == TerminationKind.DEGRADE
        assert sig5.is_degrade is True

        sig6 = StopSignal(reason=StopReason.OUTPUT_TRUNCATED)
        assert sig6.is_degrade is True

    def test_all_stop_reasons_mapped(self):
        """Every StopReason must have a TerminationKind mapping (§20)."""
        from agent_framework.models.agent import _STOP_REASON_TO_TERMINATION_KIND
        for reason in StopReason:
            assert reason in _STOP_REASON_TO_TERMINATION_KIND, (
                f"StopReason.{reason.value} missing from termination kind mapping"
            )

    def test_message_projector_adds_iteration_id(self):
        """Projected messages must carry iteration_id in metadata (§22)."""
        from agent_framework.agent.message_projector import MessageProjector
        result = IterationResult(
            iteration_index=7,
            model_response=ModelResponse(content="hello", finish_reason="stop"),
            tool_results=[
                ToolResult(tool_call_id="tc1", tool_name="f", success=True, output="ok"),
            ],
        )
        messages = MessageProjector.project_iteration(result)
        for msg in messages:
            assert msg.metadata is not None
            assert msg.metadata["iteration_id"] == 7

    def test_commit_sequencer_exists(self):
        """CommitSequencer must be available for serial commits (§25)."""
        from agent_framework.agent.commit_sequencer import CommitSequencer
        seq = CommitSequencer()
        import asyncio
        assert isinstance(seq.ordered(), asyncio.Lock)

    def test_event_bus_observation_boundary_documented(self):
        """EventBus module must document observation-only contract (§28)."""
        import agent_framework.infra.event_bus as eb_mod
        assert "MUST NOT" in eb_mod.__doc__
        assert "Mutate" in eb_mod.__doc__ or "mutate" in eb_mod.__doc__.lower()

    def test_coordinator_uses_stop_decision(self):
        """RunCoordinator must consume StopDecision, not bare bool (§19)."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        assert "stop_decision" in source
        assert "stop_decision.should_stop" in source

    def test_observation_hooks_return_none(self):
        """Observation hooks must not return control-flow values (§19)."""
        import inspect
        for hook_name in ["on_before_run", "on_iteration_started", "on_tool_call_completed", "on_final_answer"]:
            method = getattr(BaseAgent, hook_name)
            hints = inspect.get_annotations(method)
            return_type = hints.get("return", None)
            # With `from __future__ import annotations`, return type is a string
            assert return_type in (None, type(None), "None"), (
                f"{hook_name} must return None, got {return_type}"
            )


# ---------------------------------------------------------------------------
# v2.5.3: 必修 + 建议修 red-line tests
# ---------------------------------------------------------------------------

class TestV253RedLines:
    """Tests that verify v2.5.3 architectural boundaries."""

    def test_agent_loop_does_not_mutate_agent_state_status(self):
        """AgentLoop must not directly set agent_state.status (必修1)."""
        import inspect
        import agent_framework.agent.loop as loop_mod
        source = inspect.getsource(loop_mod.AgentLoop)
        assert "agent_state.status" not in source, (
            "AgentLoop must not directly mutate agent_state.status"
        )

    def test_agent_loop_does_not_mutate_token_count(self):
        """AgentLoop must not WRITE agent_state.total_tokens_used (必修1).

        Reading for logging is permitted; writing (+=, =) is prohibited.
        """
        import inspect
        import agent_framework.agent.loop as loop_mod
        source = inspect.getsource(loop_mod.AgentLoop)
        # Prohibit assignment patterns: += or =
        assert "agent_state.total_tokens_used +=" not in source, (
            "AgentLoop must not increment agent_state.total_tokens_used"
        )
        assert "agent_state.total_tokens_used =" not in source.replace("agent_state.total_tokens_used +=", ""), (
            "AgentLoop must not assign agent_state.total_tokens_used"
        )

    def test_agent_loop_does_not_import_agent_status(self):
        """AgentLoop module must not import AgentStatus (必修1)."""
        import inspect
        import agent_framework.agent.loop as loop_mod
        source = inspect.getsource(loop_mod)
        assert "import AgentStatus" not in source and "AgentStatus" not in source

    def test_run_state_controller_has_apply_iteration_result(self):
        """RunStateController must have apply_iteration_result (必修1)."""
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        assert hasattr(ctrl, "apply_iteration_result")

    def test_run_state_controller_has_set_status(self):
        """RunStateController must have set_status (必修1)."""
        from agent_framework.agent.run_state import RunStateController
        assert hasattr(RunStateController, "set_status")

    def test_run_state_controller_has_snapshot(self):
        """RunStateController must have snapshot() (必修1)."""
        from agent_framework.agent.run_state import RunStateController, AgentStateSnapshot
        ctrl = RunStateController()
        state = ctrl.initialize_state("task", "run_1")
        snap = ctrl.snapshot(state)
        assert isinstance(snap, AgentStateSnapshot)
        assert snap.run_id == "run_1"
        assert snap.task == "task"

    def test_apply_iteration_result_handles_tokens(self):
        """apply_iteration_result must account for model response tokens (必修1)."""
        from agent_framework.agent.run_state import RunStateController
        ctrl = RunStateController()
        state = ctrl.initialize_state("task", "r1")
        result = IterationResult(
            model_response=ModelResponse(
                content="hi", finish_reason="stop",
                usage=TokenUsage(total_tokens=42),
            ),
        )
        ctrl.apply_iteration_result(state, result)
        assert state.total_tokens_used == 42
        assert state.iteration_count == 1
        assert len(state.iteration_history) == 1

    def test_coordinator_uses_apply_iteration_result(self):
        """RunCoordinator must use apply_iteration_result, not advance_iteration (必修1)."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        assert "apply_iteration_result" in source

    def test_coordinator_uses_set_status(self):
        """RunCoordinator must use set_status for RUNNING status (必修1)."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        assert "set_status" in source

    def test_subagent_config_override_no_dict(self):
        """SubAgentSpec must use SubAgentConfigOverride, not dict (必修3)."""
        from agent_framework.models.subagent import SubAgentSpec
        import inspect
        hints = inspect.get_annotations(SubAgentSpec)
        # config_override must be typed, not dict
        config_type = hints.get("config_override", "")
        assert "dict" not in str(config_type).lower(), (
            f"SubAgentSpec.config_override must not be dict, got {config_type}"
        )

    def test_subagent_raw_result_exists(self):
        """SubAgentRawResult must exist for internal-only details (必修4)."""
        from agent_framework.models.subagent import SubAgentRawResult
        raw = SubAgentRawResult(
            spawn_id="s1",
            success=True,
            raw_iteration_history=[{"idx": 0}],
            internal_error_trace="traceback...",
        )
        assert raw.spawn_id == "s1"
        assert len(raw.raw_iteration_history) == 1
        assert raw.internal_error_trace is not None

    def test_delegation_summary_no_raw_fields(self):
        """DelegationSummary must not expose raw internal details (必修4)."""
        from agent_framework.models.subagent import DelegationSummary
        fields = set(DelegationSummary.model_fields.keys())
        prohibited = {"raw_iteration_history", "raw_session_messages",
                      "internal_error_trace", "debug_metadata"}
        overlap = fields & prohibited
        assert not overlap, f"DelegationSummary must not contain raw fields: {overlap}"

    def test_run_state_controller_append_user_message(self):
        """RunStateController must have append_user_message (必修1)."""
        from agent_framework.agent.run_state import RunStateController
        from agent_framework.models.session import SessionState
        ctrl = RunStateController()
        session = SessionState(session_id="s1", run_id="r1")
        msg = Message(role="user", content="hello")
        ctrl.append_user_message(session, msg)
        assert len(session.messages) == 1

    def test_all_rejection_decisions_have_reason(self):
        """All rejection decisions must contain a reason (建议测试断言)."""
        # SpawnDecision with allowed=False should have reason
        agent = BaseAgent(AgentConfig(allow_spawn_children=False))
        from agent_framework.models.subagent import SubAgentSpec
        import asyncio
        decision = asyncio.get_event_loop().run_until_complete(
            agent.on_spawn_requested(SubAgentSpec(task_input="t"))
        )
        assert decision.allowed is False
        assert decision.reason != "", "Rejection decisions must include reason"


# ---------------------------------------------------------------------------
# v2.6.1: 修复 30-34 red-line tests
# ---------------------------------------------------------------------------

class TestV261RedLines:
    """Tests that verify v2.6.1 architectural boundaries."""

    def test_resolved_run_policy_bundle_exists(self):
        """ResolvedRunPolicyBundle must be the single config source (§30)."""
        from agent_framework.agent.run_policy import ResolvedRunPolicyBundle
        from agent_framework.models.agent import CapabilityPolicy, ContextPolicy, MemoryPolicy
        bundle = ResolvedRunPolicyBundle(
            effective_run_config=EffectiveRunConfig(),
            context_policy=ContextPolicy(),
            memory_policy=MemoryPolicy(),
            capability_policy=CapabilityPolicy(),
        )
        assert bundle.effective_run_config is not None
        assert bundle.context_policy is not None
        assert bundle.memory_policy is not None
        assert bundle.capability_policy is not None

    def test_resolved_run_policy_bundle_frozen(self):
        """ResolvedRunPolicyBundle must be frozen after construction (§30)."""
        from agent_framework.agent.run_policy import ResolvedRunPolicyBundle
        from agent_framework.models.agent import CapabilityPolicy, ContextPolicy, MemoryPolicy
        bundle = ResolvedRunPolicyBundle(
            effective_run_config=EffectiveRunConfig(),
            context_policy=ContextPolicy(),
            memory_policy=MemoryPolicy(),
            capability_policy=CapabilityPolicy(),
        )
        with pytest.raises(Exception):
            bundle.context_policy = ContextPolicy()

    def test_coordinator_uses_policy_bundle(self):
        """RunCoordinator must use resolve_run_policy_bundle (§30)."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        assert "resolve_run_policy_bundle" in source
        assert "policy_bundle" in source

    def test_coordinator_does_not_construct_effective_config_directly(self):
        """RunCoordinator must not call EffectiveRunConfig() directly (§30)."""
        import inspect
        source = inspect.getsource(RunCoordinator)
        assert "EffectiveRunConfig(" not in source, (
            "RunCoordinator must not construct EffectiveRunConfig directly"
        )

    def test_decision_models_have_source_field(self):
        """All decision models must have a source field (§31)."""
        from agent_framework.models.agent import StopDecision, ToolCallDecision, SpawnDecision
        for cls in (StopDecision, ToolCallDecision, SpawnDecision):
            assert "source" in cls.model_fields, f"{cls.__name__} must have source field"

    def test_stop_decision_has_source(self):
        """StopDecision from BaseAgent must carry source (§31)."""
        agent = BaseAgent(AgentConfig(max_iterations=5))
        state = AgentState(iteration_count=5)
        result = IterationResult()
        decision = agent.should_stop(result, state)
        assert decision.should_stop is True
        assert decision.source != "", "StopDecision must carry source"

    def test_agent_run_result_has_termination_kind(self):
        """AgentRunResult must expose termination_kind (§32)."""
        result = AgentRunResult(
            stop_signal=StopSignal(reason=StopReason.LLM_STOP),
            success=True,
        )
        assert result.termination_kind == TerminationKind.NORMAL

        error_result = AgentRunResult(
            stop_signal=StopSignal(reason=StopReason.ERROR),
            success=False,
        )
        assert error_result.termination_kind == TerminationKind.ABORT

    def test_agent_run_result_distinguishes_stop_and_abort(self):
        """Run results must be distinguishable as stop vs abort (§32)."""
        normal = AgentRunResult(
            stop_signal=StopSignal(reason=StopReason.LLM_STOP), success=True,
        )
        abort = AgentRunResult(
            stop_signal=StopSignal(reason=StopReason.ERROR), success=False,
        )
        degrade = AgentRunResult(
            stop_signal=StopSignal(reason=StopReason.MAX_ITERATIONS), success=True,
        )
        assert normal.termination_kind != abort.termination_kind
        assert abort.termination_kind != degrade.termination_kind
        assert normal.termination_kind == TerminationKind.NORMAL
        assert abort.termination_kind == TerminationKind.ABORT
        assert degrade.termination_kind == TerminationKind.DEGRADE

    def test_tool_whitelist_cannot_expand_permissions(self):
        """tool_category_whitelist must only narrow, never expand (§33)."""
        from agent_framework.subagent.factory import _resolve_effective_tool_names
        from agent_framework.models.tool import ToolEntry, ToolMeta

        tools = [
            ToolEntry(meta=ToolMeta(name="calc", category="math", source="local")),
            ToolEntry(meta=ToolMeta(name="shell", category="system", source="local")),
            ToolEntry(meta=ToolMeta(name="fetch", category="network", source="local")),
            ToolEntry(meta=ToolMeta(name="search", category="general", source="local")),
        ]
        blocked = {"system", "network", "subagent"}

        # Whitelist including a blocked category should NOT expand permissions
        names = _resolve_effective_tool_names(tools, blocked, ["math", "system"])
        assert "calc" in names
        assert "shell" not in names, "whitelist must not bypass blocked categories"

    def test_tool_whitelist_narrows_from_safe_set(self):
        """whitelist only keeps intersection with safe tools (§33)."""
        from agent_framework.subagent.factory import _resolve_effective_tool_names
        from agent_framework.models.tool import ToolEntry, ToolMeta

        tools = [
            ToolEntry(meta=ToolMeta(name="calc", category="math", source="local")),
            ToolEntry(meta=ToolMeta(name="search", category="general", source="local")),
        ]
        blocked = {"system", "network", "subagent"}

        # Whitelist for math only → should exclude general
        names = _resolve_effective_tool_names(tools, blocked, ["math"])
        assert names == ["calc"]

    def test_streaming_boundary_documented(self):
        """ModelChunk must document streaming boundary (§34)."""
        from agent_framework.adapters.model.base_adapter import ModelChunk
        assert "MUST NOT" in ModelChunk.__doc__
        assert "SessionState" in ModelChunk.__doc__

    def test_session_state_streaming_boundary_documented(self):
        """SessionState must document streaming boundary (§34)."""
        from agent_framework.models.session import SessionState
        assert "ModelChunk" in SessionState.__doc__ or "Chunk" in SessionState.__doc__


# ---------------------------------------------------------------------------
# v2.6.3 §39-42: Implementation red-line tests
# ---------------------------------------------------------------------------


class TestV263RedLines:
    """Tests that verify v2.6.3 architectural boundaries."""

    # --- Fix 39: SubAgentScheduler/Runtime ownership ---

    def test_scheduler_does_not_hold_active_children(self):
        """SubAgentScheduler must not maintain active child truth source (§39)."""
        import inspect
        from agent_framework.subagent.scheduler import SubAgentScheduler
        source = inspect.getsource(SubAgentScheduler)
        assert "_active" not in source, (
            "Scheduler must not have _active dict — active_children belongs to Runtime"
        )

    def test_scheduler_does_not_have_get_active_children(self):
        """SubAgentScheduler must not expose get_active_children (§39)."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        assert not hasattr(SubAgentScheduler, "get_active_children"), (
            "get_active_children belongs to SubAgentRuntime only"
        )

    def test_runtime_is_active_children_truth_source(self):
        """SubAgentRuntime must be the sole truth source for active_children (§39)."""
        import inspect
        from agent_framework.subagent.runtime import SubAgentRuntime
        source = inspect.getsource(SubAgentRuntime)
        assert "_active" in source, "Runtime must own _active (active children truth source)"
        assert "get_active_children" in source

    def test_scheduler_allocates_task_id(self):
        """subagent_task_id must be assigned by SubAgentScheduler only (§39)."""
        from agent_framework.subagent.scheduler import SubAgentScheduler
        sched = SubAgentScheduler()
        record = sched.allocate_task_id("run_1", "spawn_x")
        assert record.subagent_task_id.startswith("task_")
        assert record.child_run_id is None, "child_run_id is runtime's job"

    def test_subagent_task_record_exists(self):
        """SubAgentTaskRecord must exist with correct fields (§39)."""
        from agent_framework.models.subagent import SubAgentTaskRecord, SubAgentTaskStatus
        record = SubAgentTaskRecord(
            subagent_task_id="task_abc",
            parent_run_id="run_1",
            spawn_id="spawn_x",
        )
        assert record.status == SubAgentTaskStatus.QUEUED
        assert record.child_run_id is None
        assert record.scheduler_decision_ref == ""

    def test_runtime_does_not_do_quota(self):
        """SubAgentRuntime must not perform quota decisions (§39)."""
        import inspect
        from agent_framework.subagent.runtime import SubAgentRuntime
        source = inspect.getsource(SubAgentRuntime)
        assert "_enforce_quota" not in source, (
            "Runtime must not enforce quota — that's scheduler's job"
        )

    # --- Fix 40: TransactionGroupIndex ---

    def test_transaction_group_index_exists(self):
        """TransactionGroupIndex must exist with correct structure (§40)."""
        from agent_framework.context.transaction_group import TransactionGroupIndex
        idx = TransactionGroupIndex()
        assert hasattr(idx, "groups_by_id")
        assert hasattr(idx, "groups_by_iteration")
        assert hasattr(idx, "message_to_group")

    def test_source_provider_accepts_transaction_index(self):
        """collect_session_groups must accept transaction_index parameter (§40)."""
        import inspect
        from agent_framework.context.source_provider import ContextSourceProvider
        sig = inspect.signature(ContextSourceProvider.collect_session_groups)
        assert "transaction_index" in sig.parameters, (
            "collect_session_groups must accept transaction_index"
        )

    def test_source_provider_consumes_index_without_rebuild(self):
        """When index is provided, provider must not generate new group IDs (§40)."""
        from agent_framework.context.source_provider import ContextSourceProvider
        from agent_framework.context.transaction_group import (
            ToolTransactionGroup,
            TransactionGroupIndex,
        )
        from agent_framework.models.message import Message
        from agent_framework.models.session import SessionState

        provider = ContextSourceProvider()
        group = ToolTransactionGroup(
            group_id="stable_id_123",
            group_type="PLAIN_MESSAGES",
            messages=[Message(role="user", content="hello")],
        )
        index = TransactionGroupIndex(
            groups_by_id={"stable_id_123": group},
            groups_by_iteration={"0": ["stable_id_123"]},
            message_to_group={"msg_1": "stable_id_123"},
        )
        session = SessionState()

        result = provider.collect_session_groups(session, transaction_index=index)
        assert len(result) == 1
        assert result[0].group_id == "stable_id_123", (
            "Provider must use existing group_id, not regenerate"
        )

    def test_source_provider_does_not_generate_group_ids_from_index(self):
        """ContextSourceProvider must not call uuid when consuming index (§40)."""
        import inspect
        from agent_framework.context.source_provider import ContextSourceProvider
        source = inspect.getsource(ContextSourceProvider._consume_transaction_index)
        assert "uuid" not in source, (
            "_consume_transaction_index must not generate new IDs"
        )

    # --- Fix 41: MemoryManager session lifecycle ---

    def test_memory_manager_has_begin_run_session(self):
        """MemoryManager must expose begin_run_session (§41)."""
        from agent_framework.memory.base_manager import BaseMemoryManager
        assert hasattr(BaseMemoryManager, "begin_run_session")

    def test_memory_manager_has_end_run_session(self):
        """MemoryManager must expose end_run_session (§41)."""
        from agent_framework.memory.base_manager import BaseMemoryManager
        assert hasattr(BaseMemoryManager, "end_run_session")

    def test_begin_end_session_pairing(self):
        """begin_run_session and end_run_session must be paired (§41)."""
        from agent_framework.memory.default_manager import DefaultMemoryManager
        store = MagicMock()
        store.list_by_user.return_value = []
        mm = DefaultMemoryManager(store)
        mm.begin_run_session("run_1", "agent_1", None)
        assert mm._session_active is True
        mm.end_run_session()
        assert mm._session_active is False

    def test_record_turn_returns_commit_decision(self):
        """record_turn must return CommitDecision (§41)."""
        from agent_framework.memory.default_manager import DefaultMemoryManager
        from agent_framework.models.memory import CommitDecision
        store = MagicMock()
        store.list_by_user.return_value = []
        mm = DefaultMemoryManager(store)
        mm.begin_run_session("run_1", "agent_1", None)
        result = mm.record_turn("hello", "world", [])
        assert isinstance(result, CommitDecision)
        assert isinstance(result.committed, bool)
        assert result.source != ""

    def test_record_turn_failure_does_not_block_end_session(self):
        """record_turn failure must not prevent end_run_session (§41)."""
        from agent_framework.memory.default_manager import DefaultMemoryManager
        store = MagicMock()
        store.list_by_user.side_effect = RuntimeError("DB exploded")
        mm = DefaultMemoryManager(store)
        mm.begin_run_session("run_1", "agent_1", None)
        # record_turn may fail, but end_run_session must still work
        try:
            mm.record_turn("hello", "world", [])
        except Exception:
            pass
        mm.end_run_session()  # Must not raise
        assert mm._session_active is False

    def test_coordinator_uses_begin_run_session(self):
        """RunCoordinator must call begin_run_session (§41)."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        assert "begin_run_session" in source

    def test_coordinator_uses_end_run_session_in_finally(self):
        """RunCoordinator must call end_run_session in finally block (§41)."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        assert "end_run_session" in source

    def test_run_session_outcome_exists(self):
        """RunSessionOutcome must exist with correct fields (§41)."""
        from agent_framework.models.memory import RunSessionOutcome
        outcome = RunSessionOutcome(
            status="completed",
            termination_kind="NORMAL",
            termination_reason="LLM stopped",
            audit_ref="run_123",
        )
        assert outcome.status == "completed"

    def test_commit_decision_exists(self):
        """CommitDecision must exist with correct fields (§41)."""
        from agent_framework.models.memory import CommitDecision
        cd = CommitDecision(committed=True, reason="test", source="test_source")
        assert cd.committed is True
        assert cd.source == "test_source"

    # --- Fix 42: RuntimeIdentityBundle ---

    def test_runtime_identity_bundle_exists(self):
        """RuntimeIdentityBundle must exist with internal/external ID separation (§42)."""
        from agent_framework.models.session import RuntimeIdentityBundle
        bundle = RuntimeIdentityBundle(
            run_id="run_1",
            run_session_id="sess_1",
            external_session_id="ext_123",
            request_id="req_456",
            user_id="user_789",
            parent_run_id="parent_run_0",
        )
        assert bundle.run_id == "run_1"
        assert bundle.external_session_id == "ext_123"
        assert bundle.parent_run_id == "parent_run_0"

    def test_runtime_identity_bundle_supports_no_external_ids(self):
        """Kernel must support running without external IDs (§42)."""
        from agent_framework.models.session import RuntimeIdentityBundle
        bundle = RuntimeIdentityBundle(run_id="run_1", run_session_id="sess_1")
        assert bundle.external_session_id is None
        assert bundle.request_id is None
        assert bundle.user_id is None

    def test_external_and_internal_ids_distinct(self):
        """External and internal IDs must be separate fields, not mixed (§42)."""
        from agent_framework.models.session import RuntimeIdentityBundle
        bundle = RuntimeIdentityBundle(
            run_id="internal_run",
            run_session_id="internal_sess",
            external_session_id="external_sess",
        )
        # Internal and external IDs must not collide in naming
        assert bundle.run_id != bundle.external_session_id
        assert bundle.run_session_id != bundle.external_session_id

    def test_runtime_identity_bundle_documented(self):
        """RuntimeIdentityBundle must have ID attribution documentation (§42)."""
        from agent_framework.models.session import RuntimeIdentityBundle
        doc = RuntimeIdentityBundle.__doc__
        assert "external_session_id" in doc
        assert "run_id" in doc
        assert "Kernel" in doc or "kernel" in doc or "Internal" in doc or "internal" in doc


# ---------------------------------------------------------------------------
# v2.6.4 §43-46: Implementation red-line tests
# ---------------------------------------------------------------------------


class TestV264RedLines:
    """Tests that verify v2.6.4 architectural boundaries."""

    # --- Fix 43: Concurrent tool side-effect commit ordering ---

    def test_tool_execution_outcome_exists(self):
        """ToolExecutionOutcome must exist with input_index (§43)."""
        from agent_framework.models.tool import ToolExecutionOutcome, ToolResult, ToolExecutionMeta
        outcome = ToolExecutionOutcome(
            tool_call_id="tc_1",
            input_index=2,
            result=ToolResult(tool_call_id="tc_1", tool_name="test", success=True),
            execution_meta=ToolExecutionMeta(),
            artifact_refs=[],
            side_effect_refs=["ref_1"],
        )
        assert outcome.input_index == 2
        assert outcome.result.success is True

    def test_tool_commit_sequencer_exists(self):
        """ToolCommitSequencer must exist (§43)."""
        from agent_framework.agent.commit_sequencer import ToolCommitSequencer
        seq = ToolCommitSequencer()
        assert hasattr(seq, "commit_outcomes")

    @pytest.mark.asyncio
    async def test_tool_commit_sequencer_orders_by_input_index(self):
        """ToolCommitSequencer must sort by input_index, not completion order (§43)."""
        from agent_framework.agent.commit_sequencer import ToolCommitSequencer
        from agent_framework.models.tool import ToolExecutionOutcome

        seq = ToolCommitSequencer()
        outcomes = [
            ToolExecutionOutcome(tool_call_id="tc_3", input_index=2),
            ToolExecutionOutcome(tool_call_id="tc_1", input_index=0),
            ToolExecutionOutcome(tool_call_id="tc_2", input_index=1),
        ]
        sorted_outcomes = await seq.commit_outcomes(outcomes)
        assert [o.input_index for o in sorted_outcomes] == [0, 1, 2]

    def test_batch_execute_documents_side_effect_boundary(self):
        """batch_execute must document side-effect commit boundary (§43)."""
        import inspect
        from agent_framework.tools.executor import ToolExecutor
        source = inspect.getsource(ToolExecutor.batch_execute)
        assert "side effect" in source.lower() or "side-effect" in source.lower()

    def test_tool_threads_must_not_write_session(self):
        """batch_execute docstring must prohibit tool thread session writes (§43)."""
        import inspect
        from agent_framework.tools.executor import ToolExecutor
        doc = inspect.getdoc(ToolExecutor.batch_execute)
        assert "MUST NOT" in doc or "must not" in doc.lower()

    # --- Fix 44: SubAgentStatus unified state machine ---

    def test_subagent_status_enum_exists(self):
        """SubAgentStatus must exist with all required values (§44)."""
        from agent_framework.models.subagent import SubAgentStatus
        assert SubAgentStatus.COMPLETED.value == "COMPLETED"
        assert SubAgentStatus.FAILED.value == "FAILED"
        assert SubAgentStatus.CANCELLED.value == "CANCELLED"
        assert SubAgentStatus.REJECTED.value == "REJECTED"
        assert SubAgentStatus.DEGRADED.value == "DEGRADED"

    def test_resolve_delegation_status_success(self):
        """Success result must resolve to COMPLETED (§44)."""
        from agent_framework.models.subagent import (
            SubAgentResult, SubAgentStatus, resolve_delegation_status,
        )
        result = SubAgentResult(spawn_id="s1", success=True)
        assert resolve_delegation_status(result) == SubAgentStatus.COMPLETED

    def test_resolve_delegation_status_timeout(self):
        """TIMEOUT error must resolve to FAILED (§44)."""
        from agent_framework.models.subagent import (
            DelegationErrorCode, SubAgentResult, SubAgentStatus,
            resolve_delegation_status,
        )
        result = SubAgentResult(spawn_id="s1", success=False, error="timed out")
        status = resolve_delegation_status(result, DelegationErrorCode.TIMEOUT)
        assert status == SubAgentStatus.FAILED

    def test_resolve_delegation_status_quota(self):
        """QUOTA_EXCEEDED must resolve to REJECTED, not FAILED (§44)."""
        from agent_framework.models.subagent import (
            DelegationErrorCode, SubAgentResult, SubAgentStatus,
            resolve_delegation_status,
        )
        result = SubAgentResult(spawn_id="s1", success=False, error="quota")
        status = resolve_delegation_status(result, DelegationErrorCode.QUOTA_EXCEEDED)
        assert status == SubAgentStatus.REJECTED

    def test_resolve_delegation_status_permission(self):
        """PERMISSION_DENIED must resolve to REJECTED (§44)."""
        from agent_framework.models.subagent import (
            DelegationErrorCode, SubAgentResult, SubAgentStatus,
            resolve_delegation_status,
        )
        result = SubAgentResult(spawn_id="s1", success=False, error="denied")
        status = resolve_delegation_status(result, DelegationErrorCode.PERMISSION_DENIED)
        assert status == SubAgentStatus.REJECTED

    def test_delegation_summary_uses_unified_status(self):
        """DelegationSummary.status must use SubAgentStatus values (§44)."""
        from agent_framework.tools.delegation import DelegationExecutor
        from agent_framework.models.subagent import SubAgentResult, SubAgentStatus
        result = SubAgentResult(spawn_id="s1", success=True, final_answer="ok")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.status == SubAgentStatus.COMPLETED.value

        failed = SubAgentResult(spawn_id="s1", success=False, error="timed out")
        summary2 = DelegationExecutor.summarize_result(failed)
        assert summary2.status == SubAgentStatus.FAILED.value

    def test_error_code_to_status_mapping_complete(self):
        """All DelegationErrorCodes must have a status mapping (§44)."""
        from agent_framework.models.subagent import (
            DelegationErrorCode, _ERROR_CODE_TO_STATUS,
        )
        for code in DelegationErrorCode:
            assert code in _ERROR_CODE_TO_STATUS, (
                f"DelegationErrorCode.{code.name} has no status mapping"
            )

    # --- Fix 45: SessionSnapshot read-only boundary ---

    def test_session_snapshot_exists(self):
        """SessionSnapshot must exist as immutable view (§45)."""
        from agent_framework.models.session import SessionSnapshot, SessionState
        session = SessionState(session_id="s1", run_id="r1")
        session.append_message(Message(role="user", content="hello"))
        snap = SessionSnapshot(session)
        assert snap.run_session_id == "s1"
        assert len(snap.messages) == 1
        assert snap.snapshot_version == 1

    def test_session_snapshot_immutable_after_creation(self):
        """SessionSnapshot must not reflect SessionState changes after creation (§45)."""
        from agent_framework.models.session import SessionSnapshot, SessionState
        session = SessionState(session_id="s1")
        session.append_message(Message(role="user", content="before"))
        snap = SessionSnapshot(session)
        # Modify session AFTER snapshot
        session.append_message(Message(role="assistant", content="after"))
        assert len(snap.messages) == 1, "Snapshot must be frozen at creation time"
        assert len(session.messages) == 2

    def test_run_state_controller_has_session_snapshot(self):
        """RunStateController must expose session_snapshot() method (§45)."""
        from agent_framework.agent.run_state import RunStateController
        assert hasattr(RunStateController, "session_snapshot")

    def test_source_provider_accepts_session_snapshot(self):
        """collect_session_groups must accept SessionSnapshot (§45)."""
        from agent_framework.context.source_provider import ContextSourceProvider
        from agent_framework.models.session import SessionSnapshot, SessionState
        session = SessionState()
        session.append_message(Message(role="user", content="test"))
        snap = SessionSnapshot(session)
        provider = ContextSourceProvider()
        groups = provider.collect_session_groups(snap)
        assert len(groups) == 1

    def test_session_snapshot_documented(self):
        """SessionSnapshot must document read-only invariants (§45)."""
        from agent_framework.models.session import SessionSnapshot
        doc = SessionSnapshot.__doc__
        assert "immutable" in doc.lower() or "read-only" in doc.lower() or "frozen" in doc.lower()

    # --- Fix 46: SubAgentFactory assembly-only boundary ---

    def test_resolved_subagent_runtime_bundle_exists(self):
        """ResolvedSubAgentRuntimeBundle must exist (§46)."""
        from agent_framework.models.subagent import ResolvedSubAgentRuntimeBundle
        bundle = ResolvedSubAgentRuntimeBundle(
            resolved_model_name="gpt-4",
            resolved_temperature=0.5,
            resolved_memory_scope="ISOLATED",
            resolved_tool_names=["calc"],
            resolved_allow_spawn_children=False,
            spawn_id="spawn_1",
        )
        assert bundle.resolved_model_name == "gpt-4"
        assert bundle.resolved_allow_spawn_children is False

    def test_factory_documents_assembly_only_contract(self):
        """SubAgentFactory must document assembly-only boundary (§46)."""
        from agent_framework.subagent.factory import SubAgentFactory
        doc = SubAgentFactory.__doc__
        assert "assembly" in doc.lower() or "Assembly" in doc
        assert "MUST NOT" in doc or "must not" in doc.lower()

    def test_factory_does_not_import_capability_policy(self):
        """SubAgentFactory module must not import CapabilityPolicy (§46)."""
        import agent_framework.subagent.factory as factory_mod
        import inspect
        source = inspect.getsource(factory_mod)
        # Check for actual imports, not docstring mentions
        assert "import CapabilityPolicy" not in source, (
            "Factory must not import CapabilityPolicy — that's policy layer's job"
        )

    def test_factory_does_not_import_effective_run_config(self):
        """SubAgentFactory module must not import EffectiveRunConfig (§46)."""
        import agent_framework.subagent.factory as factory_mod
        import inspect
        source = inspect.getsource(factory_mod)
        assert "import EffectiveRunConfig" not in source, (
            "Factory must not import EffectiveRunConfig — use resolved bundle"
        )


# ---------------------------------------------------------------------------
# v2.6.5 §47-50: Implementation red-line tests
# ---------------------------------------------------------------------------


class TestV265RedLines:
    """Tests that verify v2.6.5 architectural boundaries."""

    # --- Fix 47: Idempotency boundary for auto-retry ---

    def test_retry_safety_exists(self):
        """RetrySafety must exist with correct fields (§47)."""
        from agent_framework.models.tool import RetrySafety
        rs = RetrySafety(
            retryable=True,
            idempotent=False,
            idempotency_key="key_123",
            max_retry_attempts=3,
            retry_scope="tool",
        )
        assert rs.retryable is True
        assert rs.idempotent is False
        assert rs.idempotency_key == "key_123"

    def test_retry_decision_exists(self):
        """RetryDecision must exist with structured safety reference (§47)."""
        from agent_framework.models.tool import RetryDecision, RetrySafety
        rd = RetryDecision(
            should_retry=True,
            reason="Transient network error",
            retry_safety=RetrySafety(retryable=True, idempotent=True),
            attempt_index=1,
        )
        assert rd.should_retry is True
        assert rd.retry_safety.idempotent is True

    def test_retryable_does_not_imply_idempotent(self):
        """retryable=True must NOT imply idempotent=True (§47)."""
        from agent_framework.models.tool import RetrySafety
        rs = RetrySafety(retryable=True)
        assert rs.idempotent is False, "retryable must not auto-set idempotent"

    def test_auto_retry_requires_idempotency(self):
        """Auto-retry should require idempotency guarantee (§47)."""
        from agent_framework.models.tool import RetrySafety
        # Non-idempotent, no key → should not be auto-retried
        rs = RetrySafety(retryable=True, idempotent=False, idempotency_key=None)
        safe_for_auto = rs.idempotent or rs.idempotency_key is not None
        assert safe_for_auto is False, (
            "Non-idempotent operation without key must not be auto-retried"
        )
        # With key → safe
        rs2 = RetrySafety(retryable=True, idempotent=False, idempotency_key="key_1")
        safe2 = rs2.idempotent or rs2.idempotency_key is not None
        assert safe2 is True

    def test_tool_execution_error_has_retryable_field(self):
        """ToolExecutionError.retryable must exist (existing, verify) (§47)."""
        from agent_framework.models.tool import ToolExecutionError
        err = ToolExecutionError(
            error_type="EXECUTION_ERROR",
            retryable=True,
        )
        assert err.retryable is True

    # --- Fix 48: Checkpoint/resume stance ---

    def test_run_checkpoint_exists_as_placeholder(self):
        """RunCheckpoint must exist as placeholder (§48)."""
        from agent_framework.models.session import RunCheckpoint
        cp = RunCheckpoint(
            checkpoint_id="cp_1",
            run_id="run_1",
            run_session_id="sess_1",
        )
        assert cp.checkpoint_id == "cp_1"
        assert cp.checkpoint_version == 0

    def test_run_checkpoint_documents_no_resume(self):
        """RunCheckpoint must document that resume is NOT supported (§48)."""
        from agent_framework.models.session import RunCheckpoint
        doc = RunCheckpoint.__doc__
        assert "NOT" in doc or "not" in doc
        assert "resume" in doc.lower() or "Resume" in doc

    def test_session_state_not_resume_truth_source(self):
        """SessionState docstring must not claim resume capability (§48)."""
        from agent_framework.models.session import SessionState
        doc = SessionState.__doc__
        assert "resume" not in doc.lower(), (
            "SessionState must not claim resume support"
        )

    def test_coordinator_does_not_resume_old_run(self):
        """RunCoordinator.run() must create new run_id, never reuse (§48)."""
        import inspect
        source = inspect.getsource(RunCoordinator.run)
        # Must generate a new UUID for run_id
        assert "uuid.uuid4()" in source
        # Must not accept an existing run_id parameter for resume
        sig = inspect.signature(RunCoordinator.run)
        assert "resume_run_id" not in sig.parameters

    # --- Fix 49: Event delivery semantics ---

    def test_event_envelope_exists(self):
        """EventEnvelope must exist with event_id (§49)."""
        from agent_framework.infra.event_bus import EventEnvelope
        env = EventEnvelope(
            event_name="tool.completed",
            run_id="run_1",
            iteration_id="iter_1",
            source_layer="tool_executor",
            payload={"tool_name": "calc"},
        )
        assert env.event_id != "", "event_id must be auto-generated"
        assert env.event_name == "tool.completed"

    def test_event_envelope_has_stable_id(self):
        """Each EventEnvelope must have a unique event_id (§49)."""
        from agent_framework.infra.event_bus import EventEnvelope
        e1 = EventEnvelope(event_name="a")
        e2 = EventEnvelope(event_name="b")
        assert e1.event_id != e2.event_id

    def test_event_bus_documents_best_effort(self):
        """EventBus must document best-effort delivery (§49)."""
        from agent_framework.infra.event_bus import EventBus
        doc = EventBus.__doc__
        assert "best-effort" in doc.lower() or "Best-effort" in doc

    def test_event_bus_module_documents_idempotent_subscribers(self):
        """EventBus module must document subscriber idempotency requirement (§49)."""
        import agent_framework.infra.event_bus as eb_mod
        module_doc = eb_mod.__doc__
        assert "idempotent" in module_doc.lower()

    # --- Fix 50: Retry version chain ---

    def test_iteration_attempt_exists(self):
        """IterationAttempt must exist with version chain fields (§50)."""
        from agent_framework.models.agent import IterationAttempt
        attempt = IterationAttempt(
            attempt_id="att_1",
            iteration_id="iter_1",
            parent_attempt_id=None,
            attempt_index=0,
            trigger_reason="initial",
        )
        assert attempt.attempt_id == "att_1"
        assert attempt.parent_attempt_id is None

    def test_iteration_attempt_version_chain(self):
        """Retry attempts must form a linked chain (§50)."""
        from agent_framework.models.agent import IterationAttempt
        original = IterationAttempt(
            attempt_id="att_1", iteration_id="iter_1", attempt_index=0,
        )
        retry = IterationAttempt(
            attempt_id="att_2", iteration_id="iter_1",
            parent_attempt_id="att_1", attempt_index=1,
            trigger_reason="transient error",
        )
        assert retry.parent_attempt_id == original.attempt_id
        assert retry.iteration_id == original.iteration_id
        assert retry.attempt_index > original.attempt_index

    def test_transaction_group_attempt_exists(self):
        """TransactionGroupAttempt must exist (§50)."""
        from agent_framework.models.agent import TransactionGroupAttempt
        ga = TransactionGroupAttempt(
            group_attempt_id="ga_1",
            transaction_group_id="tg_1",
            attempt_index=0,
            status="completed",
        )
        assert ga.transaction_group_id == "tg_1"
        assert ga.parent_group_attempt_id is None

    def test_iteration_result_has_optional_attempt(self):
        """IterationResult must carry optional attempt for retry tracking (§50)."""
        from agent_framework.models.agent import IterationAttempt
        result = IterationResult(iteration_index=0)
        assert result.attempt is None  # Optional by default

        result_with_attempt = IterationResult(
            iteration_index=0,
            attempt=IterationAttempt(attempt_id="att_1", iteration_id="iter_1"),
        )
        assert result_with_attempt.attempt.attempt_id == "att_1"

    def test_retry_must_not_overwrite_original(self):
        """Retry must produce NEW IterationResult, not overwrite (§50)."""
        from agent_framework.models.agent import IterationAttempt
        original = IterationResult(
            iteration_index=0,
            attempt=IterationAttempt(attempt_id="att_1", iteration_id="iter_1", attempt_index=0),
        )
        retry = IterationResult(
            iteration_index=0,
            attempt=IterationAttempt(
                attempt_id="att_2", iteration_id="iter_1",
                parent_attempt_id="att_1", attempt_index=1,
            ),
        )
        # Both exist independently — original not overwritten
        assert original.attempt.attempt_id != retry.attempt.attempt_id
        assert retry.attempt.parent_attempt_id == original.attempt.attempt_id


# =====================================================================
# Streaming Pipeline
# =====================================================================


class TestStreamingPipeline:
    """Tests for run_stream() and execute_iteration_stream()."""

    def _make_deps(self):
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps

        mock_mm = MagicMock()
        mock_mm.select_for_context.return_value = []
        mock_mm.begin_run_session = MagicMock()
        mock_mm.end_run_session = MagicMock()
        mock_mm.begin_session = MagicMock()
        mock_mm.end_session = MagicMock()
        mock_mm.record_turn = MagicMock()

        mock_ce = MagicMock()
        mock_ce.prepare_context_for_llm = AsyncMock(return_value=[
            Message(role="system", content="sys"),
            Message(role="user", content="task"),
        ])
        mock_ce.set_skill_context = MagicMock()

        mock_tr = MagicMock()
        mock_tr.list_tools.return_value = []
        mock_tr.export_schemas.return_value = []

        mock_adapter = AsyncMock()
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)
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

    @pytest.mark.asyncio
    async def test_run_stream_yields_token_and_done(self):
        """run_stream yields TOKEN events and a final DONE event."""
        from agent_framework.adapters.model.base_adapter import ModelChunk
        from agent_framework.models.stream import StreamEventType

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        # Mock stream_complete to yield tokens
        async def mock_stream(*args, **kwargs):
            yield ModelChunk(delta_content="Hello ")
            yield ModelChunk(delta_content="world")
            yield ModelChunk(finish_reason="stop")

        deps.model_adapter.stream_complete = mock_stream

        events = []
        async for event in coordinator.run_stream(agent, deps, "Hi"):
            events.append(event)

        types = [e.type for e in events]
        assert StreamEventType.TOKEN in types
        assert StreamEventType.DONE in types
        assert types[-1] == StreamEventType.DONE

        token_texts = [e.data["text"] for e in events if e.type == StreamEventType.TOKEN]
        assert "".join(token_texts) == "Hello world"

        result = events[-1].data["result"]
        assert result.success is True
        assert result.final_answer == "Hello world"

    @pytest.mark.asyncio
    async def test_run_stream_with_tool_calls(self):
        """run_stream yields TOOL_CALL_START and TOOL_CALL_DONE events."""
        from agent_framework.adapters.model.base_adapter import ModelChunk
        from agent_framework.models.stream import StreamEventType
        import json

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        call_count = 0

        async def mock_stream(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call: tool call
                yield ModelChunk(delta_tool_calls=[{
                    "index": 0,
                    "id": "tc_1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "/tmp"})},
                }])
                yield ModelChunk(finish_reason="tool_calls")
            else:
                # Second call: final answer
                yield ModelChunk(delta_content="Done!")
                yield ModelChunk(finish_reason="stop")

        deps.model_adapter.stream_complete = mock_stream
        deps.tool_executor.batch_execute = AsyncMock(return_value=[
            (ToolResult(tool_call_id="tc_1", tool_name="read_file", success=True, output="file content"),
             ToolExecutionMeta(execution_time_ms=10, source="local")),
        ])

        events = []
        async for event in coordinator.run_stream(agent, deps, "Read file"):
            events.append(event)

        types = [e.type for e in events]
        assert StreamEventType.TOOL_CALL_START in types
        assert StreamEventType.TOOL_CALL_DONE in types
        assert StreamEventType.DONE in types

    @pytest.mark.asyncio
    async def test_run_stream_cancel(self):
        """run_stream respects cancel_event."""
        import asyncio
        from agent_framework.adapters.model.base_adapter import ModelChunk
        from agent_framework.models.stream import StreamEventType

        deps = self._make_deps()
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        cancel = asyncio.Event()
        cancel.set()  # Pre-cancelled

        events = []
        async for event in coordinator.run_stream(
            agent, deps, "Hi", cancel_event=cancel,
        ):
            events.append(event)

        # Should get DONE with USER_CANCEL, no TOKEN events
        assert len(events) == 1
        assert events[0].type == StreamEventType.DONE
        result = events[0].data["result"]
        assert result.stop_signal.reason == StopReason.USER_CANCEL

    @pytest.mark.asyncio
    async def test_execute_iteration_stream_yields_tokens(self):
        """AgentLoop.execute_iteration_stream yields TOKEN events."""
        from agent_framework.adapters.model.base_adapter import ModelChunk
        from agent_framework.models.stream import StreamEventType

        loop = AgentLoop()
        agent = DefaultAgent()

        async def mock_stream(*args, **kwargs):
            yield ModelChunk(delta_content="tok1")
            yield ModelChunk(delta_content="tok2")
            yield ModelChunk(finish_reason="stop")

        mock_adapter = AsyncMock()
        mock_adapter.stream_complete = mock_stream
        mock_executor = AsyncMock()
        mock_executor.is_tool_allowed = MagicMock(return_value=True)

        loop_deps = AgentLoopDeps(
            model_adapter=mock_adapter,
            tool_executor=mock_executor,
        )
        state = AgentState(task="test", run_id="r1")
        request = LLMRequest(
            messages=[Message(role="user", content="test")],
            tools_schema=[],
        )
        config = EffectiveRunConfig()

        events = []
        async for item in loop.execute_iteration_stream(
            agent, loop_deps, state, request, config,
        ):
            events.append(item)

        # Should have: ITERATION_START, TOKEN, TOKEN, IterationResult
        stream_events = [e for e in events if isinstance(e, StreamEvent)]
        assert any(e.type == StreamEventType.ITERATION_START for e in stream_events)
        assert any(e.type == StreamEventType.TOKEN for e in stream_events)

        # Last item is IterationResult
        assert isinstance(events[-1], IterationResult)
        assert events[-1].model_response.content == "tok1tok2"

    @pytest.mark.asyncio
    async def test_stream_event_model(self):
        """StreamEvent model basic construction."""
        from agent_framework.models.stream import StreamEvent, StreamEventType

        event = StreamEvent(
            type=StreamEventType.TOKEN,
            data={"text": "hello"},
        )
        assert event.type == StreamEventType.TOKEN
        assert event.data["text"] == "hello"

        done = StreamEvent(type=StreamEventType.DONE, data={"result": None})
        assert done.type == StreamEventType.DONE

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
from agent_framework.agent.loop import AgentLoop
from agent_framework.agent.coordinator import RunCoordinator
from agent_framework.models.agent import (
    AgentConfig,
    AgentState,
    AgentStatus,
    CapabilityPolicy,
    ContextPolicy,
    EffectiveRunConfig,
    ErrorStrategy,
    IterationResult,
    MemoryPolicy,
    Skill,
    StopReason,
    StopSignal,
)
from agent_framework.models.context import LLMRequest
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest
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
        assert await agent.on_tool_call_requested(req) is True

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
        assert await agent.on_spawn_requested(spec) is False

        agent2 = BaseAgent(AgentConfig(allow_spawn_children=True))
        assert await agent2.on_spawn_requested(spec) is True

    @pytest.mark.asyncio
    async def test_on_final_answer_noop(self):
        agent = BaseAgent(AgentConfig())
        state = AgentState(run_id="r1")
        await agent.on_final_answer("answer", state)

    def test_should_stop_on_stop_signal(self):
        agent = BaseAgent(AgentConfig(max_iterations=10))
        state = AgentState(iteration_count=0)
        result = IterationResult(stop_signal=StopSignal(reason=StopReason.LLM_STOP))
        assert agent.should_stop(result, state) is True

    def test_should_stop_on_max_iterations(self):
        agent = BaseAgent(AgentConfig(max_iterations=5))
        state = AgentState(iteration_count=5)
        result = IterationResult()
        assert agent.should_stop(result, state) is True

    def test_should_not_stop_normally(self):
        agent = BaseAgent(AgentConfig(max_iterations=10))
        state = AgentState(iteration_count=2)
        result = IterationResult()
        assert agent.should_stop(result, state) is False

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
        assert agent.should_stop(result, state) is True
        assert result.stop_signal is not None
        assert result.stop_signal.reason == StopReason.CUSTOM

    def test_should_stop_on_max_react_steps(self):
        agent = ReActAgent(max_react_steps=3)
        state = AgentState(iteration_count=3)
        result = IterationResult(model_response=ModelResponse(content="thinking...", finish_reason="stop"))
        assert agent.should_stop(result, state) is True

    def test_should_not_stop_mid_reasoning(self):
        agent = ReActAgent(max_react_steps=10)
        state = AgentState(iteration_count=2)
        response = ModelResponse(content="Let me think about this", finish_reason="tool_calls")
        result = IterationResult(model_response=response)
        assert agent.should_stop(result, state) is False

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

    def test_activate_and_deactivate(self):
        router = SkillRouter()
        skill = Skill(skill_id="math", name="Math", system_prompt_addon="Use math")
        mock_ce = MagicMock()

        router.activate_skill(skill, mock_ce)
        assert router.get_active_skill() == skill
        mock_ce.set_skill_context.assert_called_once_with("Use math")

        router.deactivate_current_skill()
        assert router.get_active_skill() is None

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

        result = await loop.execute_iteration(
            agent, mock_adapter, mock_executor, state, request, config
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
        mock_executor.batch_execute.return_value = [
            (ToolResult(tool_call_id="tc1", tool_name="search", success=True, output="found"),
             ToolExecutionMeta(execution_time_ms=10, source="local"))
        ]

        result = await loop.execute_iteration(
            agent, mock_adapter, mock_executor, state, request, config
        )
        assert result.stop_signal is None
        assert len(result.tool_results) == 1
        assert result.tool_results[0].success is True

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

        result = await loop.execute_iteration(
            agent, mock_adapter, mock_executor, state, request, config
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

        result = await loop.execute_iteration(
            agent, mock_adapter, mock_executor, state, request, config
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

        result = await loop.execute_iteration(
            agent, mock_adapter, mock_executor, state, request, config
        )
        assert result.stop_signal is not None
        assert result.stop_signal.reason == StopReason.MAX_ITERATIONS

    @pytest.mark.asyncio
    async def test_tool_blocked_by_agent_hook(self):
        """Agent's on_tool_call_requested returns False."""
        loop = AgentLoop()
        agent = self._make_agent()

        # Override hook to block
        async def deny_tool(req):
            return False
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

        result = await loop.execute_iteration(
            agent, mock_adapter, mock_executor, state, request, config
        )
        assert len(result.tool_results) == 0
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
        mock_mm.begin_session = MagicMock()
        mock_mm.end_session = MagicMock()
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
        mock_sr.get_active_skill.return_value = None
        mock_sr.deactivate_current_skill = MagicMock()

        return AgentRuntimeDeps(
            tool_registry=mock_tr,
            tool_executor=mock_executor,
            memory_manager=mock_mm,
            context_engineer=mock_ce,
            model_adapter=mock_adapter,
            skill_router=mock_sr,
        )

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
        deps.memory_manager.begin_session.assert_called_once()
        deps.memory_manager.end_session.assert_called_once()
        deps.memory_manager.record_turn.assert_called_once()

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
    async def test_skill_detection(self):
        deps = self._make_deps()
        skill = Skill(skill_id="math", name="Math", trigger_keywords=["calculate"])
        deps.skill_router.detect_skill.return_value = skill
        deps.skill_router.get_active_skill.return_value = skill

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
        deps.skill_router.activate_skill.assert_called_once()

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

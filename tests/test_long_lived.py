"""Tests for LONG_LIVED sub-agent persistent session.

Covers:
- IDLE status and transitions
- _LiveAgent storage in runtime
- send_message: session accumulation across interactions
- close_agent: explicit cleanup
- TTL eviction
- Parent run cleanup
- ToolExecutor routing for send_message/close_agent
- Config wiring
"""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.models.subagent import (
    SpawnMode,
    SubAgentStatus,
    validate_status_transition,
    InvalidStatusTransitionError,
)


# ---------------------------------------------------------------------------
# Status: IDLE
# ---------------------------------------------------------------------------

class TestIdleStatus:

    def test_idle_exists(self):
        assert SubAgentStatus.IDLE.value == "IDLE"

    def test_running_to_idle(self):
        validate_status_transition(SubAgentStatus.RUNNING, SubAgentStatus.IDLE)

    def test_idle_to_running(self):
        """send_message wakes agent from IDLE to RUNNING."""
        validate_status_transition(SubAgentStatus.IDLE, SubAgentStatus.RUNNING)

    def test_idle_to_cancelled(self):
        """close_agent can cancel an IDLE agent."""
        validate_status_transition(SubAgentStatus.IDLE, SubAgentStatus.CANCELLED)

    def test_idle_is_active(self):
        from agent_framework.models.subagent import is_active_status
        assert is_active_status(SubAgentStatus.IDLE)

    def test_idle_is_not_terminal(self):
        from agent_framework.models.subagent import is_terminal_status
        assert not is_terminal_status(SubAgentStatus.IDLE)

    def test_completed_cannot_go_to_idle(self):
        with pytest.raises(InvalidStatusTransitionError):
            validate_status_transition(SubAgentStatus.COMPLETED, SubAgentStatus.IDLE)


# ---------------------------------------------------------------------------
# _LiveAgent + Runtime pool
# ---------------------------------------------------------------------------

class TestLiveAgentPool:

    def test_runtime_has_live_agents_dict(self):
        from agent_framework.subagent.runtime import SubAgentRuntime
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agents = {}
        assert isinstance(runtime._live_agents, dict)

    def test_live_agent_dataclass(self):
        from agent_framework.subagent.runtime import _LiveAgent
        live = _LiveAgent(
            agent=MagicMock(),
            deps=MagicMock(),
        )
        assert live.interaction_count == 0
        assert len(live.session_messages) == 0

    def test_close_live_agent(self):
        from agent_framework.subagent.runtime import SubAgentRuntime, _LiveAgent
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agents = {
            "sp1": _LiveAgent(agent=MagicMock(), deps=MagicMock()),
        }
        assert runtime.close_live_agent("sp1") is True
        assert runtime.close_live_agent("sp1") is False  # Already removed

    def test_get_live_agent_status(self):
        from agent_framework.subagent.runtime import SubAgentRuntime, _LiveAgent
        from agent_framework.models.subagent import SubAgentHandle
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agents = {
            "sp1": _LiveAgent(
                agent=MagicMock(),
                deps=MagicMock(),
                handle=SubAgentHandle(status=SubAgentStatus.IDLE, spawn_id="sp1"),
                interaction_count=3,
            ),
        }
        status = runtime.get_live_agent_status("sp1")
        assert status is not None
        assert status["interaction_count"] == 3
        assert status["status"] == "IDLE"

    def test_get_live_agent_status_not_found(self):
        from agent_framework.subagent.runtime import SubAgentRuntime
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agents = {}
        assert runtime.get_live_agent_status("nonexistent") is None


# ---------------------------------------------------------------------------
# TTL eviction
# ---------------------------------------------------------------------------

class TestTTLEviction:

    def test_evict_expired(self):
        from agent_framework.subagent.runtime import SubAgentRuntime, _LiveAgent
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agent_ttl = 1  # 1 second TTL
        runtime._live_agents = {
            "sp1": _LiveAgent(
                agent=MagicMock(), deps=MagicMock(),
                last_active=time.monotonic() - 10,  # 10 seconds ago
            ),
            "sp2": _LiveAgent(
                agent=MagicMock(), deps=MagicMock(),
                last_active=time.monotonic(),  # just now
            ),
        }
        evicted = runtime.evict_expired_live_agents()
        assert evicted == 1
        assert "sp1" not in runtime._live_agents
        assert "sp2" in runtime._live_agents

    def test_evict_none_when_all_fresh(self):
        from agent_framework.subagent.runtime import SubAgentRuntime, _LiveAgent
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agent_ttl = 300
        runtime._live_agents = {
            "sp1": _LiveAgent(agent=MagicMock(), deps=MagicMock(), last_active=time.monotonic()),
        }
        assert runtime.evict_expired_live_agents() == 0


# ---------------------------------------------------------------------------
# Parent run cleanup
# ---------------------------------------------------------------------------

class TestParentRunCleanup:

    def test_cleanup_by_run_id(self):
        from agent_framework.subagent.runtime import SubAgentRuntime, _LiveAgent
        from agent_framework.models.subagent import SubAgentHandle
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agents = {
            "sp1": _LiveAgent(
                agent=MagicMock(), deps=MagicMock(),
                handle=SubAgentHandle(parent_run_id="run1"),
            ),
            "sp2": _LiveAgent(
                agent=MagicMock(), deps=MagicMock(),
                handle=SubAgentHandle(parent_run_id="run2"),
            ),
        }
        cleaned = runtime.cleanup_live_agents(parent_run_id="run1")
        assert cleaned == 1
        assert "sp1" not in runtime._live_agents
        assert "sp2" in runtime._live_agents

    def test_cleanup_all(self):
        from agent_framework.subagent.runtime import SubAgentRuntime, _LiveAgent
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agents = {
            "sp1": _LiveAgent(agent=MagicMock(), deps=MagicMock()),
            "sp2": _LiveAgent(agent=MagicMock(), deps=MagicMock()),
        }
        cleaned = runtime.cleanup_live_agents(parent_run_id=None)
        assert cleaned == 2
        assert len(runtime._live_agents) == 0


# ---------------------------------------------------------------------------
# send_message on runtime
# ---------------------------------------------------------------------------

class TestSendMessage:

    @pytest.mark.asyncio
    async def test_send_message_not_found(self):
        from agent_framework.subagent.runtime import SubAgentRuntime
        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._live_agents = {}
        result = await runtime.send_message("nonexistent", "hello")
        assert not result.success
        assert "No LONG_LIVED agent" in result.error

    @pytest.mark.asyncio
    async def test_send_message_accumulates_session(self):
        from agent_framework.subagent.runtime import SubAgentRuntime, _LiveAgent
        from agent_framework.models.subagent import SubAgentHandle
        from agent_framework.models.message import Message

        runtime = SubAgentRuntime.__new__(SubAgentRuntime)
        runtime._active = {}
        runtime._coordinator = None

        mock_agent = MagicMock()
        mock_deps = MagicMock()
        live = _LiveAgent(
            agent=mock_agent,
            deps=mock_deps,
            session_messages=[
                Message(role="user", content="first task"),
                Message(role="assistant", content="first result"),
            ],
            handle=SubAgentHandle(
                spawn_id="sp1", parent_run_id="run1",
                status=SubAgentStatus.IDLE,
            ),
            interaction_count=1,
        )
        runtime._live_agents = {"sp1": live}

        # Mock coordinator.run
        from agent_framework.models.message import TokenUsage
        mock_run_result = MagicMock()
        mock_run_result.success = True
        mock_run_result.final_answer = "second result"
        mock_run_result.error = None
        mock_run_result.usage = TokenUsage(prompt_tokens=50, completion_tokens=50, total_tokens=100)
        mock_run_result.iterations_used = 2

        mock_coordinator = AsyncMock()
        mock_coordinator.run = AsyncMock(return_value=mock_run_result)

        with patch(
            "agent_framework.agent.coordinator.RunCoordinator",
            return_value=mock_coordinator,
        ):
            result = await runtime.send_message("sp1", "second task")

        assert result.success
        assert result.final_answer == "second result"

        # Session should now have 4 messages
        assert len(live.session_messages) == 4
        assert live.session_messages[0].content == "first task"
        assert live.session_messages[1].content == "first result"
        assert live.session_messages[2].content == "second task"
        assert live.session_messages[3].content == "second result"

        # Interaction count incremented
        assert live.interaction_count == 2

        # Status back to IDLE
        assert live.handle.status == SubAgentStatus.IDLE

        # coordinator.run was called with prior session messages
        call_args = mock_coordinator.run.call_args
        initial_msgs = call_args.kwargs.get("initial_session_messages", [])
        assert len(initial_msgs) == 2  # Prior 2 messages passed as initial


# ---------------------------------------------------------------------------
# ToolExecutor routing
# ---------------------------------------------------------------------------

class TestToolExecutorRouting:

    def test_route_send_message(self):
        from agent_framework.tools.executor import ToolExecutor
        import inspect
        source = inspect.getsource(ToolExecutor._route_subagent)
        assert "send_message" in source
        assert "_subagent_send_message" in source

    def test_route_close_agent(self):
        from agent_framework.tools.executor import ToolExecutor
        import inspect
        source = inspect.getsource(ToolExecutor._route_subagent)
        assert "close_agent" in source
        assert "_subagent_close" in source


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchemas:

    def test_send_message_args(self):
        from agent_framework.tools.schemas.builtin_args import SendMessageArgs
        args = SendMessageArgs(spawn_id="sp1", message="hello")
        assert args.spawn_id == "sp1"
        assert args.message == "hello"

    def test_close_agent_args(self):
        from agent_framework.tools.schemas.builtin_args import CloseAgentArgs
        args = CloseAgentArgs(spawn_id="sp1")
        assert args.spawn_id == "sp1"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:

    def test_config_has_live_agent_fields(self):
        from agent_framework.infra.config import FrameworkConfig
        cfg = FrameworkConfig()
        assert cfg.subagent.live_agent_ttl_seconds == 300
        assert cfg.subagent.max_live_agents_per_run == 3

    def test_framework_wires_config_to_runtime(self):
        from agent_framework.entry import AgentFramework
        fw = AgentFramework()
        fw.setup()
        runtime = fw._deps.sub_agent_runtime
        if runtime is not None:
            assert runtime._live_agent_ttl == 300
            assert runtime._max_live_agents == 3


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

class TestPrompt:

    def test_prompt_teaches_long_lived(self):
        from agent_framework.agent.prompt_templates import ORCHESTRATOR_SYSTEM_PROMPT
        assert "LONG_LIVED" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "send_message" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "close_agent" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_prompt_teaches_when_to_use(self):
        from agent_framework.agent.prompt_templates import ORCHESTRATOR_SYSTEM_PROMPT
        assert "multi-turn" in ORCHESTRATOR_SYSTEM_PROMPT or "multiple rounds" in ORCHESTRATOR_SYSTEM_PROMPT

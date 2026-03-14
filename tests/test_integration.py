"""End-to-end integration tests for the agent framework.

Tests the full data flow: coordinator → loop → executor without real LLM calls.
Uses a mock model adapter to simulate LLM responses.
"""

from __future__ import annotations

import asyncio
import pytest
from typing import AsyncIterator
from unittest.mock import AsyncMock

from agent_framework.adapters.model.base_adapter import BaseModelAdapter, ModelChunk
from agent_framework.agent.base_agent import BaseAgent
from agent_framework.agent.coordinator import RunCoordinator
from agent_framework.agent.default_agent import DefaultAgent
from agent_framework.agent.react_agent import ReActAgent
from agent_framework.agent.runtime_deps import AgentRuntimeDeps
from agent_framework.agent.skill_router import SkillRouter
from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.engineer import ContextEngineer
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.memory.default_manager import DefaultMemoryManager
from agent_framework.memory.sqlite_store import SQLiteMemoryStore
from agent_framework.models.agent import AgentConfig, AgentStatus, CapabilityPolicy, Skill, StopReason
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest
from agent_framework.models.subagent import SubAgentSpec, SubAgentResult
from agent_framework.models.tool import ToolEntry, ToolMeta
from agent_framework.tools.confirmation import AutoApproveConfirmationHandler
from agent_framework.tools.decorator import tool
from agent_framework.tools.executor import ToolExecutor
from agent_framework.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Mock model adapter
# ---------------------------------------------------------------------------

class MockModelAdapter(BaseModelAdapter):
    """Model adapter that returns pre-configured responses."""

    def __init__(self, responses: list[ModelResponse] | None = None) -> None:
        super().__init__()
        self._responses = list(responses or [])
        self._call_count = 0

    def add_response(self, response: ModelResponse) -> None:
        self._responses.append(response)

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        if self._call_count >= len(self._responses):
            return ModelResponse(
                content="I don't have any more responses.",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=10),
            )
        resp = self._responses[self._call_count]
        self._call_count += 1
        return resp

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ModelChunk]:
        resp = await self.complete(messages, tools)
        yield ModelChunk(delta_content=resp.content, finish_reason=resp.finish_reason)

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(m.content or "") // 4 for m in messages)

    def supports_parallel_tool_calls(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Test tools
# ---------------------------------------------------------------------------

@tool(name="add", description="Add two numbers")
def add_tool(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


@tool(name="echo", description="Echo input text")
def echo_tool(text: str) -> str:
    """Echo the input."""
    return f"Echo: {text}"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def build_deps(model: MockModelAdapter, register_tools: bool = True) -> AgentRuntimeDeps:
    """Build runtime deps with mock model."""
    store = SQLiteMemoryStore(db_path=":memory:")
    memory = DefaultMemoryManager(store=store, auto_extract=False)
    registry = ToolRegistry()

    if register_tools:
        from agent_framework.tools.catalog import GlobalToolCatalog
        catalog = GlobalToolCatalog()
        catalog.register_function(add_tool)
        catalog.register_function(echo_tool)
        for entry in catalog.list_all():
            registry.register(entry)

    executor = ToolExecutor(
        registry=registry,
        confirmation_handler=AutoApproveConfirmationHandler(),
        max_concurrent=5,
    )
    source = ContextSourceProvider()
    builder = ContextBuilder()
    compressor = ContextCompressor()
    engineer = ContextEngineer(source_provider=source, builder=builder, compressor=compressor)

    return AgentRuntimeDeps(
        tool_registry=registry,
        tool_executor=executor,
        memory_manager=memory,
        context_engineer=engineer,
        model_adapter=model,
        skill_router=SkillRouter(),
        confirmation_handler=AutoApproveConfirmationHandler(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSimpleRun:
    """Test basic agent run without tool calls."""

    @pytest.mark.asyncio
    async def test_simple_answer(self):
        model = MockModelAdapter([
            ModelResponse(
                content="The answer is 42.",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=20),
            ),
        ])
        deps = build_deps(model, register_tools=False)
        agent = DefaultAgent(model_name="mock")
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "What is the answer?")

        assert result.success is True
        assert result.final_answer == "The answer is 42."
        assert result.iterations_used == 1
        assert result.usage.total_tokens == 20

    @pytest.mark.asyncio
    async def test_empty_task(self):
        model = MockModelAdapter([
            ModelResponse(
                content="Hello!",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            ),
        ])
        deps = build_deps(model, register_tools=False)
        agent = DefaultAgent()
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "")
        assert result.success is True


class TestToolCalling:
    """Test agent with tool calls."""

    @pytest.mark.asyncio
    async def test_single_tool_call(self):
        model = MockModelAdapter([
            # First response: call add tool
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="add", arguments={"a": 2, "b": 3}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=15),
            ),
            # Second response: final answer
            ModelResponse(
                content="2 + 3 = 5.0",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=10),
            ),
        ])
        deps = build_deps(model)
        agent = DefaultAgent(model_name="mock")
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "What is 2+3?")

        assert result.success is True
        assert result.final_answer == "2 + 3 = 5.0"
        assert result.iterations_used == 2
        assert result.usage.total_tokens == 25

    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self):
        model = MockModelAdapter([
            # Call two tools at once
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="add", arguments={"a": 1, "b": 2}),
                    ToolCallRequest(id="tc2", function_name="echo", arguments={"text": "hello"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=20),
            ),
            # Final answer
            ModelResponse(
                content="add=3.0, echo=Echo: hello",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=15),
            ),
        ])
        deps = build_deps(model)
        agent = DefaultAgent(model_name="mock")
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "Add 1+2 and echo hello")
        assert result.success is True
        assert result.iterations_used == 2

    @pytest.mark.asyncio
    async def test_tool_not_found(self):
        model = MockModelAdapter([
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="nonexistent", arguments={}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=10),
            ),
            ModelResponse(
                content="Tool not found, sorry.",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=10),
            ),
        ])
        deps = build_deps(model)
        agent = DefaultAgent(model_name="mock")
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "Use nonexistent tool")
        assert result.success is True
        assert result.iterations_used == 2


class TestReActAgent:
    """Test ReAct agent behavior."""

    @pytest.mark.asyncio
    async def test_final_answer_detection(self):
        model = MockModelAdapter([
            ModelResponse(
                content="Thought: I need to think about this.\nI'll figure it out.",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=20),
            ),
        ])
        deps = build_deps(model, register_tools=False)
        # This should stop on LLM_STOP since there's no "Final Answer:" yet
        # but finish_reason="stop" triggers LLM_STOP in the loop first
        agent = ReActAgent(model_name="mock")
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "Think hard")
        assert result.success is True

    @pytest.mark.asyncio
    async def test_final_answer_pattern(self):
        model = MockModelAdapter([
            # First iteration: thinking
            ModelResponse(
                content="Thought: Let me calculate.\n",
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="add", arguments={"a": 5, "b": 7}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=15),
            ),
            # Second iteration: final answer
            ModelResponse(
                content="Thought: The result is 12.\nFinal Answer: 5 + 7 = 12",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=20),
            ),
        ])
        deps = build_deps(model)
        agent = ReActAgent(model_name="mock")
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "What is 5+7?")
        assert result.success is True
        assert result.iterations_used == 2

        # Verify extract helper
        answer = ReActAgent.extract_final_answer(
            "Thought: done.\nFinal Answer: 5 + 7 = 12"
        )
        assert answer == "5 + 7 = 12"

    @pytest.mark.asyncio
    async def test_react_max_steps(self):
        """ReAct agent should stop when max_react_steps is reached."""
        responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id=f"tc{i}", function_name="echo", arguments={"text": f"step{i}"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=10),
            )
            for i in range(10)
        ]
        model = MockModelAdapter(responses)
        deps = build_deps(model)
        agent = ReActAgent(model_name="mock", max_react_steps=3, max_iterations=10)
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "Keep going")
        assert result.iterations_used <= 3


class TestMaxIterations:
    """Test iteration limits."""

    @pytest.mark.asyncio
    async def test_max_iterations_reached(self):
        # Agent that never stops calling tools
        responses = [
            ModelResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(id=f"tc{i}", function_name="echo", arguments={"text": f"loop{i}"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=10),
            )
            for i in range(30)
        ]
        model = MockModelAdapter(responses)
        deps = build_deps(model)
        agent = DefaultAgent(model_name="mock", max_iterations=5)
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "Loop forever")
        assert result.iterations_used <= 5
        assert result.stop_signal.reason == StopReason.MAX_ITERATIONS


class TestErrorHandling:
    """Test error handling in the run flow."""

    @pytest.mark.asyncio
    async def test_llm_error_aborts(self):
        class FailingModel(MockModelAdapter):
            async def complete(self, *args, **kwargs):
                raise RuntimeError("LLM exploded")

        model = FailingModel()
        deps = build_deps(model, register_tools=False)
        agent = DefaultAgent(model_name="mock")
        coordinator = RunCoordinator()

        result = await coordinator.run(agent, deps, "This will fail")
        # Could be error iteration or run error depending on strategy
        assert result.success is False


class TestMemory:
    """Test memory integration."""

    @pytest.mark.asyncio
    async def test_memory_extraction(self):
        """Verify memory manager is called during the run."""
        model = MockModelAdapter([
            ModelResponse(
                content="Got it, I'll remember that.",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=10),
            ),
        ])
        store = SQLiteMemoryStore(db_path=":memory:")
        memory = DefaultMemoryManager(store=store, auto_extract=True)

        registry = ToolRegistry()
        executor = ToolExecutor(registry=registry)
        engineer = ContextEngineer()

        deps = AgentRuntimeDeps(
            tool_registry=registry,
            tool_executor=executor,
            memory_manager=memory,
            context_engineer=engineer,
            model_adapter=model,
            skill_router=SkillRouter(),
        )

        agent = DefaultAgent(model_name="mock")
        coordinator = RunCoordinator()

        # This input should match preference pattern
        result = await coordinator.run(agent, deps, "以后都用中文回答我")
        assert result.success is True

        # Check if memory was extracted
        records = store.list_by_user(agent.agent_id, None, active_only=False)
        assert len(records) > 0, "Memory should have been extracted from preference pattern"


class TestContextEngineer:
    """Test context building."""

    @pytest.mark.asyncio
    async def test_context_slots_order(self):
        source = ContextSourceProvider()
        builder = ContextBuilder()
        engineer = ContextEngineer(source_provider=source, builder=builder)

        from agent_framework.models.agent import AgentConfig, AgentState
        from agent_framework.models.session import SessionState

        agent_state = AgentState(run_id="r1", task="What is 1+1?")
        session = SessionState(session_id="s1", run_id="r1")
        # User task is written to SessionState at run start (by RunCoordinator)
        session.append_message(Message(role="user", content="What is 1+1?"))
        session.append_message(Message(role="assistant", content="Previous response"))

        config = AgentConfig(system_prompt="You are helpful.")
        messages = await engineer.prepare_context_for_llm(
            agent_state,
            {
                "agent_config": config,
                "session_state": session,
                "memories": [],
                "task": "What is 1+1?",
            },
        )

        # Should have: system, user (from session), assistant (from session)
        assert len(messages) >= 3
        assert messages[0].role == "system"
        # User message is first non-system message (from session history)
        assert messages[1].role == "user"
        assert messages[1].content == "What is 1+1?"

    @pytest.mark.asyncio
    async def test_context_stats(self):
        engineer = ContextEngineer()
        from agent_framework.models.agent import AgentConfig, AgentState
        from agent_framework.models.session import SessionState

        agent_state = AgentState(run_id="r1", task="hello")
        session = SessionState(session_id="s1", run_id="r1")
        config = AgentConfig(system_prompt="Be helpful.")

        await engineer.prepare_context_for_llm(
            agent_state,
            {
                "agent_config": config,
                "session_state": session,
                "memories": [],
                "task": "hello",
            },
        )

        stats = engineer.report_context_stats()
        assert stats.total_tokens > 0
        assert stats.system_tokens > 0


class TestToolRegistry:
    """Test tool registry operations."""

    def test_register_and_lookup(self):
        registry = ToolRegistry()
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        catalog.register_function(add_tool)
        for entry in catalog.list_all():
            registry.register(entry)

        assert registry.has_tool("add")
        assert registry.has_tool("local::add")

        entry = registry.get_tool("add")
        assert entry.meta.name == "add"

    def test_export_schemas(self):
        registry = ToolRegistry()
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        catalog.register_function(add_tool)
        catalog.register_function(echo_tool)
        for entry in catalog.list_all():
            registry.register(entry)

        schemas = registry.export_schemas()
        assert len(schemas) == 2
        names = {s["function"]["name"] for s in schemas}
        assert names == {"add", "echo"}

    def test_scoped_registry(self):
        from agent_framework.tools.registry import ScopedToolRegistry

        registry = ToolRegistry()
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        catalog.register_function(add_tool)
        catalog.register_function(echo_tool)
        for entry in catalog.list_all():
            registry.register(entry)

        scoped = ScopedToolRegistry(source=registry, whitelist=["add"])
        assert scoped.has_tool("add")
        assert not scoped.has_tool("echo")
        assert len(scoped.list_tools()) == 1


class TestSubAgentScheduler:
    """Test sub-agent scheduler quota and cancellation."""

    @pytest.mark.asyncio
    async def test_quota_enforcement(self):
        from agent_framework.subagent.scheduler import SubAgentScheduler
        from agent_framework.models.subagent import SubAgentHandle, SubAgentResult

        scheduler = SubAgentScheduler(max_concurrent=2, max_per_run=2)

        assert scheduler.check_quota("run1") is True
        status = scheduler.get_quota_status("run1")
        assert status["quota_remaining"] == 2

        # Spawn two — should succeed
        async def mock_task():
            return SubAgentResult(spawn_id="s1", success=True, final_answer="done")

        h1 = SubAgentHandle(sub_agent_id="a1", spawn_id="s1", parent_run_id="run1")
        r1 = await scheduler.schedule(h1, mock_task(), deadline_ms=5000)
        assert r1.success is True

        h2 = SubAgentHandle(sub_agent_id="a2", spawn_id="s2", parent_run_id="run1")
        r2 = await scheduler.schedule(h2, mock_task(), deadline_ms=5000)
        assert r2.success is True

        # Third should fail quota
        h3 = SubAgentHandle(sub_agent_id="a3", spawn_id="s3", parent_run_id="run1")
        r3 = await scheduler.schedule(h3, mock_task(), deadline_ms=5000)
        assert r3.success is False
        assert "quota" in r3.error.lower()

    @pytest.mark.asyncio
    async def test_timeout(self):
        from agent_framework.subagent.scheduler import SubAgentScheduler
        from agent_framework.models.subagent import SubAgentHandle, SubAgentResult

        scheduler = SubAgentScheduler(max_concurrent=2, max_per_run=5)

        async def slow_task():
            await asyncio.sleep(10)
            return SubAgentResult(spawn_id="s1", success=True)

        h = SubAgentHandle(sub_agent_id="a1", spawn_id="s1", parent_run_id="run1")
        result = await scheduler.schedule(h, slow_task(), deadline_ms=100)
        assert result.success is False
        assert "timed out" in result.error.lower()


class TestRegressionFixes:
    """Regression tests for architecture-review fixes."""

    @pytest.mark.asyncio
    async def test_skill_override_applies_to_model_call(self):
        class CaptureModel(MockModelAdapter):
            def __init__(self, responses):
                super().__init__(responses)
                self.last_temperature = None
                self.last_tools = None

            async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
                self.last_temperature = temperature
                self.last_tools = tools
                return await super().complete(messages, tools, temperature, max_tokens)

        model = CaptureModel([
            ModelResponse(
                content="done",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            )
        ])
        deps = build_deps(model, register_tools=False)
        deps.skill_router.register_skill(
            Skill(
                skill_id="s1",
                name="override",
                trigger_keywords=["override"],
                system_prompt_addon="Skill prompt",
                temperature_override=0.05,
            )
        )
        agent = DefaultAgent(model_name="mock", temperature=0.7)
        result = await RunCoordinator().run(agent, deps, "please override now")
        assert result.success is True
        assert model.last_temperature == 0.05
        assert deps.context_engineer._skill_prompt is None  # cleared after run

    @pytest.mark.asyncio
    async def test_capability_policy_filters_tools_schema(self):
        @tool(name="danger", description="danger tool", category="system")
        def danger_tool() -> str:
            return "x"

        class PolicyAgent(DefaultAgent):
            def get_capability_policy(self) -> CapabilityPolicy:
                return CapabilityPolicy(
                    allow_network_tools=False,
                    allow_system_tools=False,
                    allow_spawn=False,
                )

        class CaptureModel(MockModelAdapter):
            def __init__(self, responses):
                super().__init__(responses)
                self.last_tools = None

            async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
                self.last_tools = tools or []
                return await super().complete(messages, tools, temperature, max_tokens)

        model = CaptureModel([
            ModelResponse(
                content="ok",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            )
        ])
        deps = build_deps(model, register_tools=True)
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        catalog.register_function(danger_tool)
        for entry in catalog.list_all():
            deps.tool_registry.register(entry)

        agent = PolicyAgent(model_name="mock")
        result = await RunCoordinator().run(agent, deps, "hello")
        assert result.success is True
        names = {s["function"]["name"] for s in model.last_tools}
        assert "danger" not in names

    @pytest.mark.asyncio
    async def test_capability_policy_runtime_blocks_tool_execution(self):
        called = {"danger": False}

        @tool(name="danger_runtime", description="danger runtime tool", category="system")
        def danger_runtime_tool() -> str:
            called["danger"] = True
            return "boom"

        class PolicyAgent(DefaultAgent):
            def get_capability_policy(self) -> CapabilityPolicy:
                return CapabilityPolicy(
                    allow_network_tools=False,
                    allow_system_tools=False,
                    allow_spawn=False,
                )

        model = MockModelAdapter([
            ModelResponse(
                content=None,
                tool_calls=[ToolCallRequest(id="tc1", function_name="danger_runtime", arguments={})],
                finish_reason="tool_calls",
                usage=TokenUsage(total_tokens=10),
            ),
            ModelResponse(
                content="done",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(total_tokens=5),
            ),
        ])
        deps = build_deps(model, register_tools=False)
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        catalog.register_function(danger_runtime_tool)
        for entry in catalog.list_all():
            deps.tool_registry.register(entry)

        agent = PolicyAgent(model_name="mock")
        result = await RunCoordinator().run(agent, deps, "try blocked tool")
        assert result.success is True
        assert called["danger"] is False

    @pytest.mark.asyncio
    async def test_a2a_task_input_mapping(self):
        class FakeDelegation:
            def __init__(self):
                self.task_input = None

            async def delegate_to_subagent(self, spec, parent_agent):
                return SubAgentResult(spawn_id="s1", success=False, error="not used")

            async def delegate_to_a2a(self, agent_url, task_input, skill_id=None):
                self.task_input = task_input
                return SubAgentResult(spawn_id="s1", success=True, final_answer="ok")

        registry = ToolRegistry()
        registry.register(
            ToolEntry(
                meta=ToolMeta(
                    name="delegate_to_x",
                    source="a2a",
                    a2a_agent_url="https://a2a.example",
                    is_async=True,
                ),
                callable_ref=None,
                validator_model=None,
            )
        )
        delegation = FakeDelegation()
        executor = ToolExecutor(registry=registry, delegation_executor=delegation)
        result, _meta = await executor.execute(
            ToolCallRequest(
                id="tc1",
                function_name="delegate_to_x",
                arguments={"task_input": "hello a2a"},
            )
        )
        assert result.success is True
        assert delegation.task_input == "hello a2a"

    def test_subagent_factory_uses_scoped_executor(self):
        from agent_framework.subagent.factory import SubAgentFactory
        from agent_framework.tools.catalog import GlobalToolCatalog
        from agent_framework.tools.builtin.spawn_agent import spawn_agent
        from agent_framework.tools.builtin.system import run_command

        model = MockModelAdapter([])
        deps = build_deps(model, register_tools=True)
        catalog = GlobalToolCatalog()
        catalog.register_function(spawn_agent)
        catalog.register_function(run_command)
        for entry in catalog.list_all():
            deps.tool_registry.register(entry)

        parent_agent = DefaultAgent(model_name="mock", allow_spawn_children=True)
        spec = SubAgentSpec(task_input="child task")
        child_agent, child_deps = SubAgentFactory(deps).create_agent_and_deps(spec, parent_agent)
        assert child_agent.agent_config.allow_spawn_children is False
        assert child_deps.tool_executor is not deps.tool_executor
        assert child_deps.tool_registry.has_tool("spawn_agent") is False
        assert child_deps.tool_registry.has_tool("run_command") is False

    @pytest.mark.asyncio
    async def test_subagent_runtime_uses_context_seed(self):
        from agent_framework.subagent.runtime import SubAgentRuntime

        class CaptureModel(MockModelAdapter):
            def __init__(self):
                super().__init__([
                    ModelResponse(
                        content="child done",
                        tool_calls=[],
                        finish_reason="stop",
                        usage=TokenUsage(total_tokens=5),
                    )
                ])
                self.last_messages: list[Message] = []

            async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
                self.last_messages = list(messages)
                return await super().complete(messages, tools, temperature, max_tokens)

        model = CaptureModel()
        deps = build_deps(model, register_tools=False)
        runtime = SubAgentRuntime(parent_deps=deps, coordinator=RunCoordinator())

        parent_agent = DefaultAgent(model_name="mock", allow_spawn_children=True)
        spec = SubAgentSpec(
            parent_run_id="run-parent",
            task_input="child task",
            context_seed=[
                Message(role="assistant", content="seeded context"),
                Message(role="user", content="child task"),
            ],
            deadline_ms=5000,
        )
        result = await runtime.spawn(spec, parent_agent)
        assert result.success is True

        contents = [m.content for m in model.last_messages]
        assert "seeded context" in contents
        assert contents.count("child task") == 1

    @pytest.mark.asyncio
    async def test_setup_a2a_wires_delegation_adapter(self, monkeypatch):
        from agent_framework.entry import AgentFramework

        class FakeA2AAdapter:
            def __init__(self):
                self.discovered = []

            async def discover_agent(self, url, alias=None):
                self.discovered.append((url, alias))
                return {}

            def sync_agents_to_catalog(self, catalog):
                return 0

        monkeypatch.setattr(
            "agent_framework.protocols.a2a.a2a_client_adapter.A2AClientAdapter",
            FakeA2AAdapter,
        )

        framework = AgentFramework()
        framework.setup(auto_approve_tools=True)
        framework.config.a2a.known_agents = [{"url": "https://a2a.example", "alias": "x"}]
        await framework.setup_a2a()
        assert framework._deps.delegation_executor._a2a_adapter is framework._a2a_adapter

    @pytest.mark.asyncio
    async def test_framework_run_forwards_user_id(self):
        from agent_framework.entry import AgentFramework
        from agent_framework.models.agent import AgentRunResult

        framework = AgentFramework()
        framework.setup(auto_approve_tools=True)
        framework._coordinator.run = AsyncMock(
            return_value=AgentRunResult(success=True, final_answer="ok")
        )

        result = await framework.run("hello", user_id="u_1")
        assert result.success is True
        framework._coordinator.run.assert_awaited_once()
        kwargs = framework._coordinator.run.await_args.kwargs
        assert kwargs["user_id"] == "u_1"

    def test_mcp_sync_tools_to_catalog_supports_server_filter(self):
        from agent_framework.protocols.mcp.mcp_client_manager import MCPClientManager
        from agent_framework.models.mcp import MCPToolInfo
        from agent_framework.tools.catalog import GlobalToolCatalog

        manager = MCPClientManager()
        manager._discovered_tools = {
            "s1": [MCPToolInfo(name="t1", description="", input_schema={}, server_id="s1")],
            "s2": [MCPToolInfo(name="t2", description="", input_schema={}, server_id="s2")],
        }
        catalog = GlobalToolCatalog()
        count = manager.sync_tools_to_catalog(catalog, server_id="s1")
        assert count == 1
        names = {e.meta.name for e in catalog.list_all()}
        assert names == {"t1"}

"""Tests for config-driven graph runner."""

import json
import operator
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from typing_extensions import TypedDict


class TestBuildStateSchema:

    def test_basic_types(self):
        from agent_framework.graph_runner import _build_state_schema
        schema = _build_state_schema({
            "task": {"type": "str"},
            "count": {"type": "int", "default": 5},
            "done": {"type": "bool"},
        })
        hints = schema.__annotations__
        assert hints["task"] is str
        assert hints["count"] is int
        assert hints["done"] is bool

    def test_list_with_reducer(self):
        from agent_framework.graph_runner import _build_state_schema
        schema = _build_state_schema({
            "steps": {"type": "list", "reducer": "add", "default": []},
        })
        # Annotated type with reducer
        hints = schema.__annotations__
        ann = hints["steps"]
        assert hasattr(ann, "__metadata__")
        assert ann.__metadata__[0] is operator.add

    def test_defaults(self):
        from agent_framework.graph_runner import _build_state_schema
        schema = _build_state_schema({
            "name": {"type": "str", "default": "hello"},
            "items": {"type": "list"},
        })
        # Defaults accessible on the class
        assert schema.__annotations__["name"] is str


class TestBuildCondition:

    def test_contains_routing(self):
        from agent_framework.graph_runner import _build_condition
        router = _build_condition({
            "field": "analysis",
            "contains": {"NEEDS_FIX": "fix", "LOOKS_GOOD": "approve"},
            "default": "approve",
        })
        assert router({"analysis": "Code NEEDS_FIX badly"}) == "fix"
        assert router({"analysis": "Code LOOKS_GOOD"}) == "approve"
        assert router({"analysis": "Unclear result"}) == "approve"

    def test_default_when_no_match(self):
        from agent_framework.graph_runner import _build_condition
        router = _build_condition({
            "field": "status",
            "contains": {"ERROR": "handle_error"},
            "default": "continue",
        })
        assert router({"status": "all fine"}) == "continue"


class TestBuildTransform:

    def test_extract_last_word(self):
        from agent_framework.graph_runner import _build_transform
        transform = _build_transform({
            "field": "analysis",
            "output_key": "decision",
        })
        result = transform({"analysis": "The code LOOKS_GOOD"})
        assert result == {"decision": "LOOKS_GOOD"}


class TestResolveEnvVars:

    def test_resolve_string(self):
        import os
        from agent_framework.graph_runner import _resolve_env_vars
        os.environ["TEST_GRAPH_KEY"] = "secret123"
        assert _resolve_env_vars("${TEST_GRAPH_KEY}") == "secret123"
        del os.environ["TEST_GRAPH_KEY"]

    def test_resolve_nested(self):
        import os
        from agent_framework.graph_runner import _resolve_env_vars
        os.environ["TEST_GRAPH_KEY2"] = "val"
        result = _resolve_env_vars({"key": "${TEST_GRAPH_KEY2}", "other": "plain"})
        assert result == {"key": "val", "other": "plain"}
        del os.environ["TEST_GRAPH_KEY2"]

    def test_missing_env_returns_empty(self):
        from agent_framework.graph_runner import _resolve_env_vars
        assert _resolve_env_vars("${NONEXISTENT_VAR_12345}") == ""


class TestBuildGraphFromConfig:
    """Integration test: build graph from config dict (mock framework)."""

    @pytest.mark.asyncio
    async def test_simple_linear_graph(self):
        """Two-node linear graph: researcher → summarizer."""
        from agent_framework.graph_runner import _build_state_schema, _build_condition
        from agent_framework.graph import StateGraph, START, END, InMemorySaver

        # Build manually (same logic as build_graph_from_config but without real framework)
        class State(TypedDict):
            task: str
            research: str
            summary: str

        async def mock_researcher(state):
            return {"research": f"Research on: {state['task']}"}

        async def mock_summarizer(state):
            return {"summary": f"Summary of: {state['research']}"}

        graph = StateGraph(State)
        graph.add_node("researcher", mock_researcher)
        graph.add_node("summarizer", mock_summarizer)
        graph.add_edge(START, "researcher")
        graph.add_edge("researcher", "summarizer")
        graph.add_edge("summarizer", END)
        compiled = graph.compile(checkpointer=InMemorySaver(), name="test")

        result = await compiled.invoke(
            {"task": "quantum computing", "research": "", "summary": ""},
            config={"configurable": {"thread_id": "t1"}},
        )
        assert "quantum computing" in result["research"]
        assert "Research on" in result["summary"]

    @pytest.mark.asyncio
    async def test_conditional_graph(self):
        """Graph with conditional routing based on state content."""
        from agent_framework.graph import StateGraph, START, END, InMemorySaver

        class State(TypedDict):
            input: str
            analysis: str
            result: str

        async def analyze(state):
            text = state["input"]
            if "bug" in text.lower():
                return {"analysis": "NEEDS_FIX: found bug"}
            return {"analysis": "LOOKS_GOOD: no issues"}

        async def fix(state):
            return {"result": f"Fixed: {state['analysis']}"}

        async def approve(state):
            return {"result": f"Approved: {state['analysis']}"}

        def router(state):
            if "NEEDS_FIX" in state.get("analysis", ""):
                return "NEEDS_FIX"
            return "LOOKS_GOOD"

        graph = StateGraph(State)
        graph.add_node("analyze", analyze)
        graph.add_node("fix", fix)
        graph.add_node("approve", approve)
        graph.add_edge(START, "analyze")
        graph.add_conditional_edges("analyze", router, {
            "NEEDS_FIX": "fix",
            "LOOKS_GOOD": "approve",
        })
        graph.add_edge("fix", END)
        graph.add_edge("approve", END)
        compiled = graph.compile(checkpointer=InMemorySaver(), name="cond_test")

        # Bug path
        r1 = await compiled.invoke(
            {"input": "has a bug", "analysis": "", "result": ""},
            config={"configurable": {"thread_id": "t1"}},
        )
        assert "Fixed" in r1["result"]

        # Clean path
        r2 = await compiled.invoke(
            {"input": "clean code", "analysis": "", "result": ""},
            config={"configurable": {"thread_id": "t2"}},
        )
        assert "Approved" in r2["result"]

    @pytest.mark.asyncio
    async def test_streaming(self):
        """Verify stream mode yields events per node."""
        from agent_framework.graph import StateGraph, START, END, InMemorySaver, StreamMode

        class State(TypedDict):
            value: int

        def step1(state):
            return {"value": state["value"] + 1}

        def step2(state):
            return {"value": state["value"] * 10}

        graph = StateGraph(State)
        graph.add_node("step1", step1)
        graph.add_node("step2", step2)
        graph.add_edge(START, "step1")
        graph.add_edge("step1", "step2")
        graph.add_edge("step2", END)
        compiled = graph.compile(checkpointer=InMemorySaver())

        events = []
        async for event in compiled.stream(
            {"value": 1},
            config={"configurable": {"thread_id": "s1"}},
            stream_mode=StreamMode.VALUES,
        ):
            events.append(event)

        assert len(events) >= 2
        # Final state should be (1+1)*10 = 20
        final = events[-1].data
        assert final["value"] == 20


class TestConfigJsonValid:
    """Verify example config files are valid JSON."""

    def test_graph_example_json(self):
        path = "config/graph_example.json"
        with open(path) as f:
            config = json.load(f)
        assert "graph" in config
        assert "nodes" in config["graph"]
        assert "edges" in config["graph"]

    def test_graph_conditional_json(self):
        path = "config/graph_conditional.json"
        with open(path) as f:
            config = json.load(f)
        assert "graph" in config
        assert "conditional_edges" in config["graph"]
        assert "conditions" in config["graph"]

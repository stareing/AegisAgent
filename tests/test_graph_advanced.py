"""Tests for advanced graph patterns: subgraph, map-reduce, retry, loop, parallel, timeout."""

import asyncio
import operator
from typing import Annotated

import pytest
from typing_extensions import TypedDict

from agent_framework.graph import StateGraph, START, END, InMemorySaver


# ---------------------------------------------------------------------------
# SubGraph
# ---------------------------------------------------------------------------

class TestSubGraph:

    @pytest.mark.asyncio
    async def test_subgraph_basic(self):
        """Subgraph executes as a nested graph invocation."""

        class Inner(TypedDict):
            input: str
            output: str

        inner_graph = StateGraph(Inner)
        inner_graph.add_node("process", lambda s: {"output": f"processed:{s['input']}"})
        inner_graph.add_edge(START, "process")
        inner_graph.add_edge("process", END)

        class Outer(TypedDict):
            task: str
            result: str

        outer = StateGraph(Outer)
        outer.add_node("prepare", lambda s: {"task": f"prepared:{s['task']}"})
        outer.add_subgraph(
            "inner",
            inner_graph,
            input_mapping={"task": "input"},
            output_mapping={"output": "result"},
        )
        outer.add_edge(START, "prepare")
        outer.add_edge("prepare", "inner")
        outer.add_edge("inner", END)

        compiled = outer.compile(name="subgraph_test")
        result = await compiled.invoke({"task": "hello", "result": ""})
        assert "processed:prepared:hello" == result["result"]

    @pytest.mark.asyncio
    async def test_subgraph_no_mapping(self):
        """Without mapping, subgraph shares full state."""

        class State(TypedDict):
            value: int

        inner = StateGraph(State)
        inner.add_node("double", lambda s: {"value": s["value"] * 2})
        inner.add_edge(START, "double")
        inner.add_edge("double", END)

        outer = StateGraph(State)
        outer.add_node("increment", lambda s: {"value": s["value"] + 1})
        outer.add_subgraph("sub", inner)
        outer.add_edge(START, "increment")
        outer.add_edge("increment", "sub")
        outer.add_edge("sub", END)

        result = await outer.compile().invoke({"value": 5})
        assert result["value"] == 12  # (5+1)*2


# ---------------------------------------------------------------------------
# Map-Reduce
# ---------------------------------------------------------------------------

class TestMapReduce:

    @pytest.mark.asyncio
    async def test_map_reduce_sync(self):
        """Map-reduce with sync functions."""

        class State(TypedDict):
            items: list
            total: int

        def square(item):
            return item ** 2

        def sum_all(results):
            return sum(results)

        graph = StateGraph(State)
        graph.add_map_reduce(
            "compute", map_fn=square, reduce_fn=sum_all,
            items_key="items", result_key="total",
        )
        graph.add_edge(START, "compute")
        graph.add_edge("compute", END)

        result = await graph.compile().invoke({"items": [1, 2, 3, 4], "total": 0})
        assert result["total"] == 30  # 1+4+9+16

    @pytest.mark.asyncio
    async def test_map_reduce_async(self):
        """Map-reduce with async functions."""

        class State(TypedDict):
            names: list
            greeting: str

        async def greet(name):
            return f"Hello {name}"

        async def combine(greetings):
            return "; ".join(greetings)

        graph = StateGraph(State)
        graph.add_map_reduce(
            "greet_all", map_fn=greet, reduce_fn=combine,
            items_key="names", result_key="greeting",
        )
        graph.add_edge(START, "greet_all")
        graph.add_edge("greet_all", END)

        result = await graph.compile().invoke({"names": ["Alice", "Bob"], "greeting": ""})
        assert "Hello Alice" in result["greeting"]
        assert "Hello Bob" in result["greeting"]

    @pytest.mark.asyncio
    async def test_map_reduce_empty_list(self):
        """Map-reduce with empty input list."""

        class State(TypedDict):
            items: list
            result: str

        graph = StateGraph(State)
        graph.add_map_reduce(
            "proc", map_fn=lambda x: x, reduce_fn=lambda r: "done",
            items_key="items", result_key="result",
        )
        graph.add_edge(START, "proc")
        graph.add_edge("proc", END)

        result = await graph.compile().invoke({"items": [], "result": ""})
        assert result["result"] is None  # Empty list → None


# ---------------------------------------------------------------------------
# Retry
# ---------------------------------------------------------------------------

class TestRetry:

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_failures(self):
        """Node retries on failure and eventually succeeds."""
        call_count = 0

        class State(TypedDict):
            value: int

        def flaky(state):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient error")
            return {"value": 42}

        graph = StateGraph(State)
        graph.add_retry_node("flaky", flaky, max_retries=3)
        graph.add_edge(START, "flaky")
        graph.add_edge("flaky", END)

        result = await graph.compile().invoke({"value": 0})
        assert result["value"] == 42
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausted(self):
        """Raises after exhausting retries."""

        class State(TypedDict):
            value: int

        def always_fail(state):
            raise RuntimeError("permanent")

        graph = StateGraph(State)
        graph.add_retry_node("fail", always_fail, max_retries=2)
        graph.add_edge(START, "fail")
        graph.add_edge("fail", END)

        from agent_framework.graph.errors import GraphRuntimeError
        with pytest.raises(GraphRuntimeError, match="permanent"):
            await graph.compile().invoke({"value": 0})

    @pytest.mark.asyncio
    async def test_retry_selective_exception(self):
        """Only retries on specified exception types."""
        call_count = 0

        class State(TypedDict):
            value: int

        def selective_fail(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TypeError("wrong type")
            return {"value": 1}

        graph = StateGraph(State)
        graph.add_retry_node("sel", selective_fail, max_retries=3, retry_on=(ValueError,))
        graph.add_edge(START, "sel")
        graph.add_edge("sel", END)

        # TypeError not in retry_on → should raise immediately
        from agent_framework.graph.errors import GraphRuntimeError
        with pytest.raises(GraphRuntimeError):
            await graph.compile().invoke({"value": 0})


# ---------------------------------------------------------------------------
# Loop
# ---------------------------------------------------------------------------

class TestLoop:

    @pytest.mark.asyncio
    async def test_loop_with_condition(self):
        """Loop executes until condition returns exit."""

        class State(TypedDict):
            counter: int

        def increment(state):
            return {"counter": state["counter"] + 1}

        def check(state):
            return "exit" if state["counter"] >= 5 else "continue"

        graph = StateGraph(State)
        graph.add_loop("count", body_fn=increment, condition_fn=check)
        graph.add_edge(START, "count_body")
        compiled = graph.compile(recursion_limit=50)

        result = await compiled.invoke({"counter": 0})
        assert result["counter"] == 5

    @pytest.mark.asyncio
    async def test_loop_max_iterations(self):
        """Loop stops at max_iterations even if condition says continue."""

        class State(TypedDict):
            counter: int

        def increment(state):
            return {"counter": state["counter"] + 1}

        graph = StateGraph(State)
        graph.add_loop(
            "infinite", body_fn=increment,
            condition_fn=lambda s: "continue",
            max_iterations=3,
        )
        graph.add_edge(START, "infinite_body")
        compiled = graph.compile(recursion_limit=50)

        result = await compiled.invoke({"counter": 0})
        assert result["counter"] == 3


# ---------------------------------------------------------------------------
# Parallel Branches
# ---------------------------------------------------------------------------

class TestParallelBranches:

    @pytest.mark.asyncio
    async def test_fan_out_fan_in(self):
        """Multiple branches execute in parallel, then join."""

        class State(TypedDict):
            input: str
            branch_a: str
            branch_b: str
            merged: str

        graph = StateGraph(State)
        graph.add_node("start", lambda s: {"input": s["input"].upper()})
        graph.add_parallel_branches(
            entry_node="start",
            branches={
                "analyze": lambda s: {"branch_a": f"analysis:{s['input']}"},
                "translate": lambda s: {"branch_b": f"translated:{s['input']}"},
            },
            join_node="merge",
            join_fn=lambda s: {"merged": f"{s['branch_a']} + {s['branch_b']}"},
        )
        graph.add_edge(START, "start")
        graph.add_edge("merge", END)

        result = await graph.compile().invoke({
            "input": "hello", "branch_a": "", "branch_b": "", "merged": "",
        })
        assert "analysis:HELLO" in result["merged"]
        assert "translated:HELLO" in result["merged"]


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

class TestTimeout:

    @pytest.mark.asyncio
    async def test_timeout_succeeds(self):
        """Node completes within timeout."""

        class State(TypedDict):
            value: str

        async def fast(state):
            return {"value": "done"}

        graph = StateGraph(State)
        graph.add_timeout_node("fast", fast, timeout_seconds=5.0)
        graph.add_edge(START, "fast")
        graph.add_edge("fast", END)

        result = await graph.compile().invoke({"value": ""})
        assert result["value"] == "done"

    @pytest.mark.asyncio
    async def test_timeout_fallback(self):
        """Node exceeds timeout, returns fallback."""

        class State(TypedDict):
            value: str

        async def slow(state):
            await asyncio.sleep(10)
            return {"value": "should not reach"}

        graph = StateGraph(State)
        graph.add_timeout_node(
            "slow", slow, timeout_seconds=0.1,
            fallback={"value": "timed_out"},
        )
        graph.add_edge(START, "slow")
        graph.add_edge("slow", END)

        result = await graph.compile().invoke({"value": ""})
        assert result["value"] == "timed_out"


# ---------------------------------------------------------------------------
# Complex DAG: combining multiple patterns
# ---------------------------------------------------------------------------

class TestComplexDAG:

    @pytest.mark.asyncio
    async def test_pipeline_with_retry_and_conditional(self):
        """Retry → conditional routing → parallel branches → join."""
        call_count = 0

        class State(TypedDict):
            input: str
            validated: str
            path_a: str
            path_b: str
            result: str

        def validate(state):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("transient")
            return {"validated": f"ok:{state['input']}"}

        def router(state):
            if "error" in state.get("validated", ""):
                return "error_path"
            return "normal_path"

        graph = StateGraph(State)
        graph.add_retry_node("validate", validate, max_retries=2)
        graph.add_node("path_a", lambda s: {"path_a": f"A:{s['validated']}"})
        graph.add_node("path_b", lambda s: {"path_b": f"B:{s['validated']}"})
        graph.add_node("join", lambda s: {"result": f"{s.get('path_a', '')}|{s.get('path_b', '')}"})

        graph.add_edge(START, "validate")
        graph.add_conditional_edges("validate", router, {
            "normal_path": "path_a",
            "error_path": "path_b",
        })
        graph.add_edge("path_a", "join")
        graph.add_edge("path_b", "join")
        graph.add_edge("join", END)

        result = await graph.compile().invoke({
            "input": "test", "validated": "", "path_a": "", "path_b": "", "result": "",
        })
        assert "A:ok:test" in result["result"]
        assert call_count == 2  # Retried once

    @pytest.mark.asyncio
    async def test_map_reduce_then_conditional(self):
        """Map-reduce → conditional → different endpoints."""

        class State(TypedDict):
            items: list
            total: int
            result: str

        graph = StateGraph(State)
        graph.add_map_reduce(
            "sum_items", map_fn=lambda x: x * 2, reduce_fn=sum,
            items_key="items", result_key="total",
        )
        graph.add_node("big", lambda s: {"result": f"big:{s['total']}"})
        graph.add_node("small", lambda s: {"result": f"small:{s['total']}"})

        graph.add_edge(START, "sum_items")
        graph.add_conditional_edges("sum_items", lambda s: "big" if s["total"] > 10 else "small")
        graph.add_edge("big", END)
        graph.add_edge("small", END)

        r1 = await graph.compile().invoke({"items": [1, 2, 3], "total": 0, "result": ""})
        assert r1["result"] == "big:12"  # 2+4+6 = 12 > 10 → big

        r2 = await graph.compile().invoke({"items": [1, 2], "total": 0, "result": ""})
        assert r2["result"] == "small:6"  # 2+4 = 6 ≤ 10 → small

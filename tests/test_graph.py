"""Comprehensive tests for agent_framework.graph — LangGraph-style graph execution."""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated
from typing_extensions import TypedDict

import pytest

from agent_framework.graph import (
    END,
    START,
    CompiledGraph,
    DuplicateNodeError,
    GraphCompilationError,
    GraphRuntimeError,
    GraphStreamEvent,
    InMemorySaver,
    InvalidEdgeError,
    InvalidStateUpdateError,
    NodeNotFoundError,
    NoPathToEndError,
    StateGraph,
    StreamMode,
    UnreachableNodeError,
    apply_update,
    extract_reducers,
    passthrough_node,
    tool_node,
)


# ── Test state schemas ─────────────────────────────────────────────


class SimpleState(TypedDict):
    value: int
    name: str


class ListState(TypedDict):
    items: Annotated[list[str], operator.add]
    count: int


class MultiReducerState(TypedDict):
    messages: Annotated[list[str], operator.add]
    total: Annotated[int, lambda old, new: old + new]
    label: str


# ══════════════════════════════════════════════════════════════════
# State & Reducer Tests
# ══════════════════════════════════════════════════════════════════


class TestReducerExtraction:
    """Test state schema introspection and reducer resolution."""

    def test_no_reducers(self):
        reducers = extract_reducers(SimpleState)
        assert reducers == {"value": None, "name": None}

    def test_single_reducer(self):
        reducers = extract_reducers(ListState)
        assert reducers["items"] is operator.add
        assert reducers["count"] is None

    def test_multiple_reducers(self):
        reducers = extract_reducers(MultiReducerState)
        assert reducers["messages"] is operator.add
        assert reducers["total"] is not None  # lambda
        assert reducers["label"] is None

    def test_reducer_application(self):
        reducers = extract_reducers(ListState)
        state = {"items": ["a"], "count": 0}
        update = {"items": ["b", "c"], "count": 5}
        result = apply_update(state, update, reducers)
        assert result == {"items": ["a", "b", "c"], "count": 5}

    def test_apply_update_no_mutation(self):
        reducers = extract_reducers(SimpleState)
        original = {"value": 1, "name": "old"}
        result = apply_update(original, {"value": 2}, reducers)
        assert original["value"] == 1  # not mutated
        assert result["value"] == 2

    def test_custom_lambda_reducer(self):
        reducers = extract_reducers(MultiReducerState)
        state = {"messages": [], "total": 10, "label": "x"}
        result = apply_update(state, {"total": 5}, reducers)
        assert result["total"] == 15  # 10 + 5

    def test_apply_update_new_key(self):
        reducers = extract_reducers(SimpleState)
        state = {"value": 1, "name": "a"}
        result = apply_update(state, {"extra": "bonus"}, reducers)
        assert result["extra"] == "bonus"


# ══════════════════════════════════════════════════════════════════
# Graph Builder Tests
# ══════════════════════════════════════════════════════════════════


class TestStateGraphBuilder:
    """Test StateGraph builder methods."""

    def test_add_node_name_fn(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {"value": 1})
        assert "a" in g._nodes

    def test_add_node_fn_only(self):
        def my_node(state):
            return {}

        g = StateGraph(SimpleState)
        g.add_node(my_node)
        assert "my_node" in g._nodes

    def test_add_node_fluent(self):
        g = StateGraph(SimpleState)
        result = g.add_node("a", lambda s: {})
        assert result is g  # fluent API

    def test_duplicate_node_raises(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        with pytest.raises(DuplicateNodeError):
            g.add_node("a", lambda s: {})

    def test_reserved_name_raises(self):
        g = StateGraph(SimpleState)
        with pytest.raises(InvalidEdgeError):
            g.add_node(START, lambda s: {})
        with pytest.raises(InvalidEdgeError):
            g.add_node(END, lambda s: {})

    def test_add_edge(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_node("b", lambda s: {})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        assert len(g._edges) == 3

    def test_edge_to_unknown_node_raises(self):
        g = StateGraph(SimpleState)
        with pytest.raises(NodeNotFoundError):
            g.add_edge(START, "nonexistent")

    def test_start_as_target_raises(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        with pytest.raises(InvalidEdgeError):
            g.add_edge("a", START)

    def test_end_as_source_raises(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        with pytest.raises(InvalidEdgeError):
            g.add_edge(END, "a")

    def test_set_entry_and_finish(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.set_entry_point("a")
        g.set_finish_point("a")
        assert len(g._edges) == 2

    def test_add_conditional_edges(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_node("b", lambda s: {})
        g.add_conditional_edges(
            START,
            lambda s: "go_a",
            {"go_a": "a", "go_b": "b"},
        )
        assert len(g._conditional_edges) == 1


# ══════════════════════════════════════════════════════════════════
# Compilation & Validation Tests
# ══════════════════════════════════════════════════════════════════


class TestCompilation:
    """Test compile-time topology validation."""

    def test_basic_compile(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        compiled = g.compile()
        assert isinstance(compiled, CompiledGraph)

    def test_no_start_edge_raises(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_edge("a", END)
        with pytest.raises(GraphCompilationError, match="no edges from START"):
            g.compile()

    def test_unreachable_node_raises(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_node("b", lambda s: {})  # unreachable
        g.add_edge(START, "a")
        g.add_edge("a", END)
        g.add_edge("b", END)
        with pytest.raises(UnreachableNodeError):
            g.compile()

    def test_no_path_to_end_raises(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_node("b", lambda s: {})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        # b has no edge to END
        g.add_edge("a", END)
        with pytest.raises(NoPathToEndError):
            g.compile()

    def test_conditional_edge_compile(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_node("b", lambda s: {})
        g.add_conditional_edges(
            START,
            lambda s: "go_a",
            {"go_a": "a", "go_b": "b"},
        )
        g.add_edge("a", END)
        g.add_edge("b", END)
        compiled = g.compile()
        assert isinstance(compiled, CompiledGraph)

    def test_compile_with_name(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        compiled = g.compile(name="TestGraph")
        assert compiled.name == "TestGraph"


# ══════════════════════════════════════════════════════════════════
# Invoke Tests
# ══════════════════════════════════════════════════════════════════


class TestInvoke:
    """Test compiled graph invoke() execution."""

    @pytest.mark.asyncio
    async def test_single_node(self):
        g = StateGraph(SimpleState)
        g.add_node("increment", lambda s: {"value": s["value"] + 1})
        g.add_edge(START, "increment")
        g.add_edge("increment", END)
        app = g.compile()

        result = await app.invoke({"value": 0, "name": "test"})
        assert result["value"] == 1
        assert result["name"] == "test"

    @pytest.mark.asyncio
    async def test_linear_chain(self):
        g = StateGraph(SimpleState)
        g.add_node("step1", lambda s: {"value": s["value"] + 1})
        g.add_node("step2", lambda s: {"value": s["value"] * 10})
        g.add_node("step3", lambda s: {"name": f"result={s['value']}"})
        g.add_edge(START, "step1")
        g.add_edge("step1", "step2")
        g.add_edge("step2", "step3")
        g.add_edge("step3", END)
        app = g.compile()

        result = await app.invoke({"value": 5, "name": ""})
        assert result["value"] == 60  # (5+1)*10
        assert result["name"] == "result=60"

    @pytest.mark.asyncio
    async def test_list_reducer(self):
        g = StateGraph(ListState)
        g.add_node("add_a", lambda s: {"items": ["A"], "count": 1})
        g.add_node("add_b", lambda s: {"items": ["B"], "count": 2})
        g.add_edge(START, "add_a")
        g.add_edge("add_a", "add_b")
        g.add_edge("add_b", END)
        app = g.compile()

        result = await app.invoke({"items": [], "count": 0})
        assert result["items"] == ["A", "B"]  # operator.add appends
        assert result["count"] == 2  # last-write-wins

    @pytest.mark.asyncio
    async def test_async_node(self):
        async def async_step(state):
            await asyncio.sleep(0.01)
            return {"value": state["value"] + 100}

        g = StateGraph(SimpleState)
        g.add_node("async_step", async_step)
        g.add_edge(START, "async_step")
        g.add_edge("async_step", END)
        app = g.compile()

        result = await app.invoke({"value": 0, "name": "async"})
        assert result["value"] == 100

    @pytest.mark.asyncio
    async def test_conditional_routing(self):
        def router(state):
            return "positive" if state["value"] > 0 else "negative"

        g = StateGraph(SimpleState)
        g.add_node("positive", lambda s: {"name": "pos"})
        g.add_node("negative", lambda s: {"name": "neg"})
        g.add_edge(START, "positive")  # Dummy — overridden by conditional
        g.add_edge(START, "negative")  # Dummy — overridden by conditional

        # Actually, let's use a proper setup
        g2 = StateGraph(SimpleState)
        g2.add_node("check", lambda s: {})  # no-op, just for routing
        g2.add_node("positive", lambda s: {"name": "pos"})
        g2.add_node("negative", lambda s: {"name": "neg"})
        g2.add_edge(START, "check")
        g2.add_conditional_edges(
            "check",
            router,
            {"positive": "positive", "negative": "negative"},
        )
        g2.add_edge("positive", END)
        g2.add_edge("negative", END)
        app = g2.compile()

        result_pos = await app.invoke({"value": 5, "name": ""})
        assert result_pos["name"] == "pos"

        result_neg = await app.invoke({"value": -1, "name": ""})
        assert result_neg["name"] == "neg"

    @pytest.mark.asyncio
    async def test_conditional_no_mapping(self):
        """Router returns node names directly (no mapping dict)."""

        def router(state):
            return "add" if state["count"] < 3 else END

        g = StateGraph(ListState)
        g.add_node("add", lambda s: {"items": [str(s["count"])], "count": s["count"] + 1})
        g.add_edge(START, "add")
        g.add_conditional_edges("add", router)
        app = g.compile()

        result = await app.invoke({"items": [], "count": 0})
        assert result["items"] == ["0", "1", "2"]
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_loop_with_condition(self):
        """Graph that loops until a condition is met."""

        def router(state):
            return "loop" if state["value"] < 10 else "done"

        g = StateGraph(SimpleState)
        g.add_node("loop", lambda s: {"value": s["value"] + 3})
        g.add_node("done", lambda s: {"name": "finished"})
        g.add_edge(START, "loop")
        g.add_conditional_edges(
            "loop", router, {"loop": "loop", "done": "done"}
        )
        g.add_edge("done", END)
        app = g.compile()

        result = await app.invoke({"value": 0, "name": ""})
        assert result["value"] == 12  # 0→3→6→9→12 (12 >= 10 → done)
        assert result["name"] == "finished"

    @pytest.mark.asyncio
    async def test_recursion_limit(self):
        """Infinite loop hits recursion limit."""
        g = StateGraph(SimpleState)
        g.add_node("loop", lambda s: {"value": s["value"] + 1})
        g.add_edge(START, "loop")
        g.add_conditional_edges("loop", lambda s: "loop")
        # Need END path for compilation
        g.add_edge("loop", END)
        # But the router always returns "loop", so it won't reach END
        # ... actually this won't compile because the conditional overrides.
        # Let me adjust: compile allows it since "loop" has a direct edge to END too.
        app = g.compile(recursion_limit=5)

        with pytest.raises(GraphRuntimeError, match="Recursion limit"):
            await app.invoke({"value": 0, "name": ""})

    @pytest.mark.asyncio
    async def test_node_returning_none(self):
        """Node returning None is treated as empty update."""
        g = StateGraph(SimpleState)
        g.add_node("noop", lambda s: None)
        g.add_edge(START, "noop")
        g.add_edge("noop", END)
        app = g.compile()

        result = await app.invoke({"value": 42, "name": "kept"})
        assert result == {"value": 42, "name": "kept"}

    @pytest.mark.asyncio
    async def test_node_invalid_return_raises(self):
        """Node returning non-dict raises InvalidStateUpdateError."""
        g = StateGraph(SimpleState)
        g.add_node("bad", lambda s: "not a dict")
        g.add_edge(START, "bad")
        g.add_edge("bad", END)
        app = g.compile()

        with pytest.raises(GraphRuntimeError):
            await app.invoke({"value": 0, "name": ""})

    @pytest.mark.asyncio
    async def test_node_exception_raises(self):
        def explode(state):
            raise ValueError("boom")

        g = StateGraph(SimpleState)
        g.add_node("explode", explode)
        g.add_edge(START, "explode")
        g.add_edge("explode", END)
        app = g.compile()

        with pytest.raises(GraphRuntimeError, match="boom"):
            await app.invoke({"value": 0, "name": ""})

    @pytest.mark.asyncio
    async def test_default_state_init(self):
        """Invoke with None input uses schema defaults."""
        g = StateGraph(SimpleState)
        g.add_node("check", lambda s: {"name": f"v={s['value']}"})
        g.add_edge(START, "check")
        g.add_edge("check", END)
        app = g.compile()

        result = await app.invoke()
        assert result["value"] == 0
        assert result["name"] == "v=0"


# ══════════════════════════════════════════════════════════════════
# Fan-out / Fan-in Tests
# ══════════════════════════════════════════════════════════════════


class TestFanOutFanIn:
    """Test parallel execution (fan-out → fan-in)."""

    @pytest.mark.asyncio
    async def test_parallel_branches_with_reducer(self):
        """Two branches execute in parallel, results merged via reducer."""

        class FanState(TypedDict):
            aggregate: Annotated[list[str], operator.add]

        g = StateGraph(FanState)
        g.add_node("a", lambda s: {"aggregate": ["A"]})
        g.add_node("b", lambda s: {"aggregate": ["B"]})
        g.add_node("c", lambda s: {"aggregate": ["C"]})
        g.add_node("d", lambda s: {"aggregate": ["D"]})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("a", "c")
        g.add_edge("b", "d")
        g.add_edge("c", "d")
        g.add_edge("d", END)
        app = g.compile()

        result = await app.invoke({"aggregate": []})
        # A runs first, then B and C fan-out, then D
        assert "A" in result["aggregate"]
        assert "B" in result["aggregate"]
        assert "C" in result["aggregate"]
        assert "D" in result["aggregate"]


# ══════════════════════════════════════════════════════════════════
# Stream Tests
# ══════════════════════════════════════════════════════════════════


class TestStream:
    """Test compiled graph stream() execution."""

    @pytest.mark.asyncio
    async def test_stream_values_mode(self):
        g = StateGraph(SimpleState)
        g.add_node("step1", lambda s: {"value": 1})
        g.add_node("step2", lambda s: {"value": 2})
        g.add_edge(START, "step1")
        g.add_edge("step1", "step2")
        g.add_edge("step2", END)
        app = g.compile()

        events = []
        async for event in app.stream({"value": 0, "name": ""}):
            events.append(event)

        assert len(events) == 2
        assert events[0].node == "step1"
        assert events[0].data["value"] == 1
        assert events[1].node == "step2"
        assert events[1].data["value"] == 2

    @pytest.mark.asyncio
    async def test_stream_updates_mode(self):
        g = StateGraph(SimpleState)
        g.add_node("step1", lambda s: {"value": 10})
        g.add_edge(START, "step1")
        g.add_edge("step1", END)
        app = g.compile()

        events = []
        async for event in app.stream(
            {"value": 0, "name": ""}, stream_mode=StreamMode.UPDATES
        ):
            events.append(event)

        assert len(events) == 1
        assert events[0].data == {"value": 10}  # partial update only

    @pytest.mark.asyncio
    async def test_stream_debug_mode(self):
        g = StateGraph(SimpleState)
        g.add_node("step1", lambda s: {"value": 42})
        g.add_edge(START, "step1")
        g.add_edge("step1", END)
        app = g.compile()

        events = []
        async for event in app.stream(
            {"value": 0, "name": ""}, stream_mode=StreamMode.DEBUG
        ):
            events.append(event)

        assert len(events) == 1
        assert "state" in events[0].data
        assert "update" in events[0].data
        assert "duration_ms" in events[0].data
        assert events[0].data["state"]["value"] == 42

    @pytest.mark.asyncio
    async def test_stream_string_mode(self):
        """Stream mode accepts string value."""
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {"value": 1})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        app = g.compile()

        events = []
        async for event in app.stream({"value": 0, "name": ""}, stream_mode="updates"):
            events.append(event)
        assert len(events) == 1


# ══════════════════════════════════════════════════════════════════
# Checkpointer Tests
# ══════════════════════════════════════════════════════════════════


class TestCheckpointing:
    """Test InMemorySaver and checkpointing integration."""

    @pytest.mark.asyncio
    async def test_in_memory_saver_basic(self):
        saver = InMemorySaver()
        await saver.save("t1", {"value": 42}, "node_a", 0)
        loaded = await saver.load("t1")
        assert loaded == {"value": 42}

    @pytest.mark.asyncio
    async def test_in_memory_saver_missing_thread(self):
        saver = InMemorySaver()
        assert await saver.load("nonexistent") is None

    @pytest.mark.asyncio
    async def test_checkpointer_saves_during_invoke(self):
        saver = InMemorySaver()

        g = StateGraph(SimpleState)
        g.add_node("step1", lambda s: {"value": s["value"] + 1})
        g.add_node("step2", lambda s: {"value": s["value"] * 2})
        g.add_edge(START, "step1")
        g.add_edge("step1", "step2")
        g.add_edge("step2", END)
        app = g.compile(checkpointer=saver)

        config = {"configurable": {"thread_id": "thread-1"}}
        result = await app.invoke({"value": 5, "name": ""}, config)
        assert result["value"] == 12  # (5+1)*2

        # Checkpoint should have the final state
        checkpoint = saver.get_checkpoint("thread-1")
        assert checkpoint is not None
        assert checkpoint["state"]["value"] == 12

    @pytest.mark.asyncio
    async def test_checkpointer_stream(self):
        saver = InMemorySaver()

        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {"value": 10})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        app = g.compile(checkpointer=saver)

        config = {"configurable": {"thread_id": "t-stream"}}
        async for _ in app.stream({"value": 0, "name": ""}, config):
            pass

        checkpoint = saver.get_checkpoint("t-stream")
        assert checkpoint is not None


# ══════════════════════════════════════════════════════════════════
# Batch Tests
# ══════════════════════════════════════════════════════════════════


class TestBatch:
    """Test abatch() for concurrent multi-input execution."""

    @pytest.mark.asyncio
    async def test_abatch_basic(self):
        g = StateGraph(SimpleState)
        g.add_node("double", lambda s: {"value": s["value"] * 2})
        g.add_edge(START, "double")
        g.add_edge("double", END)
        app = g.compile()

        results = await app.abatch([
            {"value": 1, "name": "a"},
            {"value": 2, "name": "b"},
            {"value": 3, "name": "c"},
        ])
        assert [r["value"] for r in results] == [2, 4, 6]


# ══════════════════════════════════════════════════════════════════
# Introspection Tests
# ══════════════════════════════════════════════════════════════════


class TestIntrospection:
    """Test graph structure introspection."""

    def test_get_graph_structure(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_node("b", lambda s: {})
        g.add_edge(START, "a")
        g.add_edge("a", "b")
        g.add_edge("b", END)
        app = g.compile()

        structure = app.get_graph_structure()
        assert set(structure["nodes"]) == {"a", "b"}
        assert len(structure["edges"]) == 3

    def test_repr(self):
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_edge(START, "a")
        g.add_edge("a", END)
        app = g.compile(name="MyGraph")
        assert "MyGraph" in repr(app)


# ══════════════════════════════════════════════════════════════════
# Node Helper Tests
# ══════════════════════════════════════════════════════════════════


class TestNodeHelpers:
    """Test pre-built node factories."""

    @pytest.mark.asyncio
    async def test_tool_node_with_keys(self):
        def double(x):
            return x * 2

        node = tool_node(double, input_key="value", output_key="result")

        result = await node({"value": 5, "name": "test"})
        assert result == {"result": 10}

    @pytest.mark.asyncio
    async def test_tool_node_full_state(self):
        def extract(state):
            return {"name": state["name"].upper()}

        node = tool_node(extract)
        result = await node({"value": 1, "name": "hello"})
        assert result == {"name": "HELLO"}

    @pytest.mark.asyncio
    async def test_tool_node_async(self):
        async def async_double(x):
            return x * 2

        node = tool_node(async_double, input_key="value", output_key="result")
        result = await node({"value": 7})
        assert result == {"result": 14}

    def test_passthrough_noop(self):
        node = passthrough_node()
        result = node({"value": 42})
        assert result == {}

    def test_passthrough_transform(self):
        node = passthrough_node(lambda s: {"value": s["value"] + 1})
        result = node({"value": 5})
        assert result == {"value": 6}


# ══════════════════════════════════════════════════════════════════
# Integration: Real-world Patterns
# ══════════════════════════════════════════════════════════════════


class TestRealWorldPatterns:
    """Test patterns commonly used with LangGraph."""

    @pytest.mark.asyncio
    async def test_rag_pattern(self):
        """Simulated RAG: retrieve → grade → generate/transform loop."""

        class RAGState(TypedDict):
            query: str
            documents: Annotated[list[str], operator.add]
            answer: str
            retries: int

        def retrieve(state):
            return {"documents": [f"doc_for_{state['query']}"]}

        def grade(state):
            return {}  # just routing

        def grade_router(state):
            if state["documents"] and "good" in state["query"]:
                return "generate"
            if state["retries"] >= 2:
                return "generate"
            return "transform"

        def transform(state):
            return {"query": state["query"] + "_refined", "retries": state["retries"] + 1}

        def generate(state):
            return {"answer": f"Answer based on {len(state['documents'])} docs"}

        g = StateGraph(RAGState)
        g.add_node("retrieve", retrieve)
        g.add_node("grade", grade)
        g.add_node("transform", transform)
        g.add_node("generate", generate)

        g.add_edge(START, "retrieve")
        g.add_edge("retrieve", "grade")
        g.add_conditional_edges(
            "grade", grade_router,
            {"generate": "generate", "transform": "transform"},
        )
        g.add_edge("transform", "retrieve")
        g.add_edge("generate", END)
        app = g.compile()

        # "good" query — should go directly to generate
        result = await app.invoke({"query": "good_question", "documents": [], "answer": "", "retries": 0})
        assert "Answer based on" in result["answer"]

        # Bad query — needs 2 retries then generates
        result = await app.invoke({"query": "bad", "documents": [], "answer": "", "retries": 0})
        assert result["retries"] == 2
        assert "Answer based on" in result["answer"]

    @pytest.mark.asyncio
    async def test_chatbot_with_tool_routing(self):
        """Simulated agent: decide → tool/respond → check → end/continue."""

        class ChatState(TypedDict):
            messages: Annotated[list[str], operator.add]
            tool_calls: int

        def decide(state):
            return {}

        def decide_router(state):
            if state["tool_calls"] < 2:
                return "use_tool"
            return "respond"

        def use_tool(state):
            return {
                "messages": [f"Tool result #{state['tool_calls'] + 1}"],
                "tool_calls": state["tool_calls"] + 1,
            }

        def respond(state):
            return {"messages": ["Final response"]}

        g = StateGraph(ChatState)
        g.add_node("decide", decide)
        g.add_node("use_tool", use_tool)
        g.add_node("respond", respond)

        g.add_edge(START, "decide")
        g.add_conditional_edges(
            "decide", decide_router,
            {"use_tool": "use_tool", "respond": "respond"},
        )
        g.add_edge("use_tool", "decide")
        g.add_edge("respond", END)
        app = g.compile()

        result = await app.invoke({"messages": ["User: Hello"], "tool_calls": 0})
        assert result["tool_calls"] == 2
        assert "Final response" in result["messages"]
        assert len(result["messages"]) == 4  # User + 2 tools + final

    @pytest.mark.asyncio
    async def test_multi_reducer_accumulation(self):
        """Test custom reducers across multiple iterations."""
        g = StateGraph(MultiReducerState)
        g.add_node("step1", lambda s: {"messages": ["hello"], "total": 10, "label": "one"})
        g.add_node("step2", lambda s: {"messages": ["world"], "total": 20, "label": "two"})
        g.add_edge(START, "step1")
        g.add_edge("step1", "step2")
        g.add_edge("step2", END)
        app = g.compile()

        result = await app.invoke({"messages": [], "total": 0, "label": ""})
        assert result["messages"] == ["hello", "world"]
        assert result["total"] == 30  # 0 + 10 + 20
        assert result["label"] == "two"  # last-write-wins


# ══════════════════════════════════════════════════════════════════
# Edge Cases
# ══════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_conditional_router_invalid_key(self):
        """Router returns a key not in the mapping."""
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_edge(START, "a")
        g.add_conditional_edges(
            "a", lambda s: "invalid_key", {"valid": END}
        )
        g.add_edge("a", END)
        app = g.compile()

        with pytest.raises(GraphRuntimeError, match="not in mapping"):
            await app.invoke({"value": 0, "name": ""})

    @pytest.mark.asyncio
    async def test_router_returns_END(self):
        """Router returns END directly (no mapping)."""

        def router(state):
            return END if state["value"] > 0 else "loop"

        g = StateGraph(SimpleState)
        g.add_node("loop", lambda s: {"value": s["value"] - 1})
        g.add_edge(START, "loop")
        g.add_conditional_edges("loop", router)
        app = g.compile()

        # loop(3) → value=2, router sees value=2 > 0 → END
        result = await app.invoke({"value": 3, "name": ""})
        assert result["value"] == 2

    @pytest.mark.asyncio
    async def test_state_isolation_between_nodes(self):
        """Ensure nodes get state copies, not shared references."""
        mutations = []

        def mutating_node(state):
            state["name"] = "mutated"  # This mutates the copy, not original
            mutations.append(True)
            return {"value": 99}

        g = StateGraph(SimpleState)
        g.add_node("mutate", mutating_node)
        g.add_node("check", lambda s: {"name": f"checked:{s['name']}"})
        g.add_edge(START, "mutate")
        g.add_edge("mutate", "check")
        g.add_edge("check", END)
        app = g.compile()

        result = await app.invoke({"value": 0, "name": "original"})
        # The mutation inside mutating_node should NOT affect the real state
        assert result["name"] == "checked:original"
        assert result["value"] == 99

    @pytest.mark.asyncio
    async def test_empty_graph_single_passthrough(self):
        """Minimal valid graph: START → node → END with no state change."""
        g = StateGraph(SimpleState)
        g.add_node("pass", lambda s: {})
        g.add_edge(START, "pass")
        g.add_edge("pass", END)
        app = g.compile()

        result = await app.invoke({"value": 7, "name": "x"})
        assert result == {"value": 7, "name": "x"}

    @pytest.mark.asyncio
    async def test_router_returns_invalid_type(self):
        """Router returning non-str/list raises error."""
        g = StateGraph(SimpleState)
        g.add_node("a", lambda s: {})
        g.add_edge(START, "a")
        g.add_conditional_edges("a", lambda s: 42)  # type: ignore[arg-type]
        g.add_edge("a", END)
        app = g.compile()

        with pytest.raises(GraphRuntimeError, match="invalid type"):
            await app.invoke({"value": 0, "name": ""})

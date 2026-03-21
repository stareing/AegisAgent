"""StateGraph — the builder class for defining graph topologies.

Usage mirrors LangGraph::

    from agent_framework.graph import StateGraph, START, END

    class MyState(TypedDict):
        messages: Annotated[list[str], operator.add]
        count: int

    graph = StateGraph(MyState)
    graph.add_node("greet", greet_fn)
    graph.add_edge(START, "greet")
    graph.add_edge("greet", END)
    compiled = graph.compile()
    result = await compiled.invoke({"messages": [], "count": 0})
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, Sequence

from agent_framework.graph.constants import END, START
from agent_framework.graph.errors import (DuplicateNodeError, InvalidEdgeError,
                                          NodeNotFoundError)

if TYPE_CHECKING:
    from agent_framework.graph.compiled import CompiledGraph


# Type aliases
NodeFn = Callable[..., Any]  # sync or async (state) -> partial update
RouterFn = Callable[..., str | list[str]]  # (state) -> next node name(s)


class _EdgeDef:
    """Internal representation of a single edge."""

    __slots__ = ("source", "target")

    def __init__(self, source: str, target: str) -> None:
        self.source = source
        self.target = target


class _ConditionalEdgeDef:
    """Internal representation of a conditional edge (router)."""

    __slots__ = ("source", "router_fn", "mapping", "then")

    def __init__(
        self,
        source: str,
        router_fn: RouterFn,
        mapping: dict[str, str] | None = None,
        then: str | None = None,
    ) -> None:
        self.source = source
        self.router_fn = router_fn
        self.mapping = mapping
        self.then = then


class _NodeDef:
    """Internal representation of a registered node."""

    __slots__ = ("name", "fn", "is_async", "metadata")

    def __init__(self, name: str, fn: NodeFn, metadata: dict[str, Any] | None = None) -> None:
        self.name = name
        self.fn = fn
        self.is_async = inspect.iscoroutinefunction(fn)
        self.metadata = metadata or {}


class StateGraph:
    """Builder for constructing a compiled state graph.

    Mirrors LangGraph's ``StateGraph`` API surface:
    - ``add_node(name, fn)``
    - ``add_edge(source, target)``
    - ``add_conditional_edges(source, router, mapping, then)``
    - ``set_entry_point(name)`` (alias for ``add_edge(START, name)``)
    - ``set_finish_point(name)`` (alias for ``add_edge(name, END)``)
    - ``compile(**kwargs)`` → ``CompiledGraph``
    """

    def __init__(self, state_schema: type) -> None:
        self._state_schema = state_schema
        self._nodes: dict[str, _NodeDef] = {}
        self._edges: list[_EdgeDef] = []
        self._conditional_edges: list[_ConditionalEdgeDef] = []

    # ── Node registration ──────────────────────────────────────────

    def add_node(
        self,
        name_or_fn: str | NodeFn,
        fn: NodeFn | None = None,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> StateGraph:
        """Register a node.

        Supports two calling conventions (matching LangGraph):
        - ``add_node("name", fn)``
        - ``add_node(fn)``  — uses ``fn.__name__`` as the node name
        """
        if callable(name_or_fn) and fn is None:
            # add_node(fn) form
            node_fn = name_or_fn
            node_name = getattr(node_fn, "__name__", str(node_fn))
        elif isinstance(name_or_fn, str) and fn is not None:
            node_name = name_or_fn
            node_fn = fn
        else:
            raise TypeError(
                "add_node() requires (name, fn) or (fn,). "
                f"Got: ({type(name_or_fn).__name__}, {type(fn).__name__})"
            )

        if node_name in self._nodes:
            raise DuplicateNodeError(node_name)
        if node_name in (START, END):
            raise InvalidEdgeError(f"Cannot use reserved name '{node_name}' as a node")

        self._nodes[node_name] = _NodeDef(node_name, node_fn, metadata)
        return self  # fluent API

    # ── Edge registration ──────────────────────────────────────────

    def add_edge(self, source: str, target: str) -> StateGraph:
        """Add a direct edge from *source* to *target*.

        Either endpoint may be ``START`` or ``END``.
        """
        self._validate_edge_endpoint(source, allow_start=True, allow_end=False)
        self._validate_edge_endpoint(target, allow_start=False, allow_end=True)
        self._edges.append(_EdgeDef(source, target))
        return self

    def add_conditional_edges(
        self,
        source: str,
        router: RouterFn,
        mapping: dict[str, str] | None = None,
        *,
        then: str | None = None,
    ) -> StateGraph:
        """Add conditional routing from *source* via *router* function.

        Args:
            source: The node whose output feeds the router.
            router: ``(state) -> str | list[str]`` returning the chosen
                branch key(s).
            mapping: Optional ``{router_return_value: target_node}`` dict.
                If ``None``, the router must return valid node names directly.
            then: Optional node to always execute after the routed target.
                Useful for fan-out → fan-in patterns.
        """
        self._validate_edge_endpoint(source, allow_start=True, allow_end=False)
        self._conditional_edges.append(
            _ConditionalEdgeDef(source, router, mapping, then)
        )
        return self

    # ── Advanced: Sub-graph, Map-Reduce, Retry, Loop ─────────────

    def add_subgraph(
        self,
        name: str,
        subgraph: StateGraph,
        input_mapping: dict[str, str] | None = None,
        output_mapping: dict[str, str] | None = None,
    ) -> StateGraph:
        """Add a compiled sub-graph as a single node.

        The sub-graph executes as a nested graph invocation. Input/output
        mappings translate between parent and child state keys.

        Args:
            name: Node name in the parent graph.
            input_mapping: ``{parent_key: child_key}`` for feeding child state.
            output_mapping: ``{child_key: parent_key}`` for collecting results.
        """
        compiled_sub = subgraph.compile(name=f"sub:{name}")
        in_map = input_mapping or {}
        out_map = output_mapping or {}

        async def _subgraph_node(state: dict) -> dict:
            child_state = {}
            if in_map:
                for parent_key, child_key in in_map.items():
                    if parent_key in state:
                        child_state[child_key] = state[parent_key]
            else:
                child_state = dict(state)

            result = await compiled_sub.invoke(child_state)

            update = {}
            if out_map:
                for child_key, parent_key in out_map.items():
                    if child_key in result:
                        update[parent_key] = result[child_key]
            else:
                update = result
            return update

        _subgraph_node.__name__ = f"subgraph:{name}"
        return self.add_node(name, _subgraph_node, metadata={"type": "subgraph"})

    def add_map_reduce(
        self,
        name: str,
        map_fn: NodeFn,
        reduce_fn: NodeFn,
        items_key: str,
        result_key: str,
        max_concurrency: int = 10,
    ) -> StateGraph:
        """Add a map-reduce node that processes list items in parallel.

        Reads ``state[items_key]`` (a list), applies ``map_fn`` to each item
        concurrently, then passes all results through ``reduce_fn``.

        Args:
            name: Node name.
            map_fn: ``(item) -> result`` applied to each item.
            reduce_fn: ``(results: list) -> final`` aggregates map outputs.
            items_key: State key containing the input list.
            result_key: State key to write the reduced result.
            max_concurrency: Max parallel map invocations.
        """
        import asyncio as _asyncio

        is_map_async = inspect.iscoroutinefunction(map_fn)
        is_reduce_async = inspect.iscoroutinefunction(reduce_fn)

        async def _map_reduce_node(state: dict) -> dict:
            items = state.get(items_key, [])
            if not items:
                return {result_key: None}

            sem = _asyncio.Semaphore(max_concurrency)

            async def _run_one(item: Any) -> Any:
                async with sem:
                    if is_map_async:
                        return await map_fn(item)
                    return map_fn(item)

            mapped = await _asyncio.gather(*[_run_one(item) for item in items])
            mapped_list = list(mapped)

            if is_reduce_async:
                reduced = await reduce_fn(mapped_list)
            else:
                reduced = reduce_fn(mapped_list)

            return {result_key: reduced}

        _map_reduce_node.__name__ = f"map_reduce:{name}"
        return self.add_node(name, _map_reduce_node, metadata={"type": "map_reduce"})

    def add_retry_node(
        self,
        name: str,
        fn: NodeFn,
        max_retries: int = 3,
        retry_on: tuple[type[Exception], ...] = (Exception,),
    ) -> StateGraph:
        """Add a node with automatic retry on failure.

        Args:
            name: Node name.
            fn: The node function.
            max_retries: Maximum retry attempts.
            retry_on: Exception types that trigger retry.
        """
        is_async = inspect.iscoroutinefunction(fn)

        async def _retry_node(state: dict) -> dict:
            last_error: Exception | None = None
            for attempt in range(max_retries + 1):
                try:
                    if is_async:
                        return await fn(state)
                    return fn(state)
                except retry_on as exc:
                    last_error = exc
                    if attempt < max_retries:
                        continue
            raise last_error  # type: ignore[misc]

        _retry_node.__name__ = f"retry:{name}"
        return self.add_node(name, _retry_node, metadata={
            "type": "retry", "max_retries": max_retries,
        })

    def add_loop(
        self,
        name: str,
        body_fn: NodeFn,
        condition_fn: RouterFn,
        exit_key: str = "exit",
        max_iterations: int = 10,
    ) -> StateGraph:
        """Add a self-looping node that repeats until condition returns exit_key.

        Internally creates two nodes: ``{name}_body`` and ``{name}_check``,
        with a conditional edge routing back to body or forward.

        Args:
            name: Base name for the loop nodes.
            body_fn: The loop body function.
            condition_fn: ``(state) -> str`` returning exit_key to stop, anything else to continue.
            exit_key: The router return value that exits the loop.
            max_iterations: Safety limit on loop iterations.
        """
        body_name = f"{name}_body"
        check_name = f"{name}_check"
        counter_key = f"_loop_{name}_count"

        is_body_async = inspect.iscoroutinefunction(body_fn)

        async def _counted_body(state: dict) -> dict:
            count = state.get(counter_key, 0)
            if count >= max_iterations:
                return {counter_key: count}
            if is_body_async:
                result = await body_fn(state)
            else:
                result = body_fn(state)
            result = result or {}
            result[counter_key] = count + 1
            return result

        def _loop_router(state: dict) -> str:
            count = state.get(counter_key, 0)
            if count >= max_iterations:
                return exit_key
            result = condition_fn(state)
            return result

        _counted_body.__name__ = body_name
        _loop_router.__name__ = check_name

        self.add_node(body_name, _counted_body, metadata={"type": "loop_body"})
        self.add_node(check_name, lambda state: {}, metadata={"type": "loop_check"})
        self.add_edge(body_name, check_name)
        # check_name routes back to body or exits
        self._conditional_edges.append(
            _ConditionalEdgeDef(check_name, _loop_router, {exit_key: END, "continue": body_name})
        )
        return self

    def add_parallel_branches(
        self,
        entry_node: str,
        branches: dict[str, NodeFn],
        join_node: str,
        join_fn: NodeFn,
    ) -> StateGraph:
        """Add explicit parallel branches with a join point.

        Creates edges: entry_node → [branch_1, branch_2, ...] → join_node.
        All branches execute concurrently (fan-out), results merged, then
        join_fn processes the merged state (fan-in).

        Args:
            entry_node: Node that feeds all branches (must already exist).
            branches: ``{name: fn}`` branch node definitions.
            join_node: Name for the join/merge node.
            join_fn: Function that processes the merged branch results.
        """
        # Register join node first so branch→join edges are valid
        self.add_node(join_node, join_fn, metadata={"type": "join"})

        for branch_name, branch_fn in branches.items():
            self.add_node(branch_name, branch_fn)
            self.add_edge(entry_node, branch_name)
            self.add_edge(branch_name, join_node)
        return self

    def add_timeout_node(
        self,
        name: str,
        fn: NodeFn,
        timeout_seconds: float = 30.0,
        fallback: dict | None = None,
    ) -> StateGraph:
        """Add a node with a timeout. Returns fallback on timeout.

        Args:
            name: Node name.
            fn: The node function.
            timeout_seconds: Max execution time.
            fallback: State update to return on timeout. Defaults to empty dict.
        """
        import asyncio as _asyncio

        is_async = inspect.iscoroutinefunction(fn)

        async def _timeout_node(state: dict) -> dict:
            try:
                if is_async:
                    return await _asyncio.wait_for(fn(state), timeout=timeout_seconds)
                return fn(state)
            except _asyncio.TimeoutError:
                return fallback or {}

        _timeout_node.__name__ = f"timeout:{name}"
        return self.add_node(name, _timeout_node, metadata={
            "type": "timeout", "timeout_seconds": timeout_seconds,
        })

    # ── Convenience aliases ────────────────────────────────────────

    def set_entry_point(self, name: str) -> StateGraph:
        """Alias for ``add_edge(START, name)``."""
        return self.add_edge(START, name)

    def set_finish_point(self, name: str) -> StateGraph:
        """Alias for ``add_edge(name, END)``."""
        return self.add_edge(name, END)

    # ── Compile ────────────────────────────────────────────────────

    def compile(
        self,
        *,
        checkpointer: Any | None = None,
        name: str | None = None,
        recursion_limit: int | None = None,
        interrupt_before: Sequence[str] | None = None,
        interrupt_after: Sequence[str] | None = None,
    ) -> "CompiledGraph":
        """Validate the topology and produce an executable ``CompiledGraph``.

        Args:
            checkpointer: Optional persistence backend for state snapshots.
            name: Human-readable name for the compiled graph.
            recursion_limit: Max node invocations per run (default 25).
            interrupt_before: Node names where execution pauses BEFORE running.
            interrupt_after: Node names where execution pauses AFTER running.
        """
        # Lazy import to avoid circular reference
        from agent_framework.graph.compiled import CompiledGraph

        return CompiledGraph(
            state_schema=self._state_schema,
            nodes=dict(self._nodes),
            edges=list(self._edges),
            conditional_edges=list(self._conditional_edges),
            checkpointer=checkpointer,
            name=name or "StateGraph",
            recursion_limit=recursion_limit,
            interrupt_before=list(interrupt_before or []),
            interrupt_after=list(interrupt_after or []),
        )

    # ── Internal helpers ───────────────────────────────────────────

    def _validate_edge_endpoint(
        self, name: str, *, allow_start: bool, allow_end: bool
    ) -> None:
        if name == START:
            if not allow_start:
                raise InvalidEdgeError("START cannot be used as a target")
            return
        if name == END:
            if not allow_end:
                raise InvalidEdgeError("END cannot be used as a source")
            return
        if name not in self._nodes:
            raise NodeNotFoundError(name)

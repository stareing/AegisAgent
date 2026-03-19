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

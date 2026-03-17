"""CompiledGraph — the executable graph produced by StateGraph.compile().

Execution model:
1. Topological validation at construction time
2. ``invoke(input)`` — run graph to completion, return final state
3. ``stream(input)`` — async generator yielding per-node events
4. Fan-out / fan-in — multiple edges from one source execute targets
   concurrently via asyncio.gather, then merge state updates via reducers
5. Conditional edges — router function selects next node(s)
6. Checkpointing — optional persistence after each node (for HITL / resume)
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections import defaultdict
from typing import Any, AsyncGenerator

from agent_framework.graph.constants import DEFAULT_RECURSION_LIMIT, END, START, StreamMode
from agent_framework.graph.errors import (
    GraphCompilationError,
    GraphRuntimeError,
    InvalidStateUpdateError,
    NoPathToEndError,
    UnreachableNodeError,
)
from agent_framework.graph.state import apply_update, extract_reducers, get_default_state

# Internal edge defs imported from graph module (same package)
from agent_framework.graph.graph import _ConditionalEdgeDef, _EdgeDef, _NodeDef


# ── Stream event payloads ──────────────────────────────────────────

class GraphStreamEvent:
    """Event yielded by ``CompiledGraph.stream()``.

    Attributes:
        node: Name of the node that just executed (or ``__start__`` / ``__end__``).
        data: Payload — depends on ``stream_mode``:
            VALUES → full state snapshot after the node
            UPDATES → partial dict returned by the node
            DEBUG → ``{"state": ..., "update": ..., "duration_ms": ...}``
    """

    __slots__ = ("node", "data", "stream_mode")

    def __init__(self, node: str, data: dict[str, Any], stream_mode: StreamMode) -> None:
        self.node = node
        self.data = data
        self.stream_mode = stream_mode

    def __repr__(self) -> str:
        return f"GraphStreamEvent(node={self.node!r}, mode={self.stream_mode.value})"


# ── Checkpointer protocol ─────────────────────────────────────────

class CheckpointerProtocol:
    """Optional persistence backend for graph state.

    Implement ``save`` and ``load`` to enable resume-from-checkpoint.
    The default ``None`` checkpointer means no persistence.
    """

    async def save(self, thread_id: str, state: dict[str, Any], node: str, step: int) -> None:
        """Persist state after a node execution."""

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        """Load the most recent state for a thread, or None."""
        return None


class InMemorySaver(CheckpointerProtocol):
    """Simple in-memory checkpointer (matching LangGraph's ``MemorySaver``)."""

    def __init__(self) -> None:
        self._store: dict[str, dict[str, Any]] = {}

    async def save(self, thread_id: str, state: dict[str, Any], node: str, step: int) -> None:
        self._store[thread_id] = {
            "state": copy.deepcopy(state),
            "node": node,
            "step": step,
        }

    async def load(self, thread_id: str) -> dict[str, Any] | None:
        entry = self._store.get(thread_id)
        return copy.deepcopy(entry["state"]) if entry else None

    def get_checkpoint(self, thread_id: str) -> dict[str, Any] | None:
        """Synchronous accessor for inspection / tests."""
        return copy.deepcopy(self._store.get(thread_id))


# ── Compiled graph ─────────────────────────────────────────────────

class CompiledGraph:
    """Immutable, executable graph produced by ``StateGraph.compile()``.

    Thread-safe: invoke/stream create per-call state copies.
    """

    def __init__(
        self,
        *,
        state_schema: type,
        nodes: dict[str, _NodeDef],
        edges: list[_EdgeDef],
        conditional_edges: list[_ConditionalEdgeDef],
        checkpointer: Any | None = None,
        name: str = "StateGraph",
        recursion_limit: int | None = None,
        interrupt_before: list[str] | None = None,
        interrupt_after: list[str] | None = None,
    ) -> None:
        self._state_schema = state_schema
        self._nodes = nodes
        self._edges = edges
        self._conditional_edges = conditional_edges
        self._checkpointer = checkpointer
        self.name = name
        self._recursion_limit = recursion_limit or DEFAULT_RECURSION_LIMIT
        self._interrupt_before = set(interrupt_before or [])
        self._interrupt_after = set(interrupt_after or [])

        # Pre-compute reducers from schema
        self._reducers = extract_reducers(state_schema)

        # Build adjacency structures for fast traversal
        self._direct_edges: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            self._direct_edges[edge.source].append(edge.target)

        self._cond_edges: dict[str, list[_ConditionalEdgeDef]] = defaultdict(list)
        for ce in conditional_edges:
            self._cond_edges[ce.source].append(ce)

        # Validate topology at compile time
        self._validate()

    # ── Topology validation ────────────────────────────────────────

    def _validate(self) -> None:
        """Ensure graph is structurally sound."""
        all_node_names = set(self._nodes.keys())

        # 1. Must have at least one edge from START
        start_targets = self._direct_edges.get(START, [])
        start_conds = self._cond_edges.get(START, [])
        if not start_targets and not start_conds:
            raise GraphCompilationError("Graph has no edges from START")

        # 2. Reachability from START (BFS)
        reachable = self._bfs_reachable(START)
        unreachable = all_node_names - reachable
        if unreachable:
            raise UnreachableNodeError(unreachable)

        # 3. All nodes must have a path to END (reverse BFS)
        can_reach_end = self._reverse_bfs_to_end()
        no_path = all_node_names - can_reach_end
        if no_path:
            raise NoPathToEndError(no_path)

    def _bfs_reachable(self, source: str) -> set[str]:
        """Return all node names reachable from *source* (excluding sentinels)."""
        visited: set[str] = set()
        queue = [source]
        while queue:
            current = queue.pop(0)
            for target in self._direct_edges.get(current, []):
                if target != END and target not in visited:
                    visited.add(target)
                    queue.append(target)
            for ce in self._cond_edges.get(current, []):
                targets = self._all_conditional_targets(ce)
                for t in targets:
                    if t != END and t not in visited:
                        visited.add(t)
                        queue.append(t)
        return visited

    def _reverse_bfs_to_end(self) -> set[str]:
        """Return all nodes that can reach END via reverse traversal."""
        # Build reverse adjacency
        reverse: dict[str, list[str]] = defaultdict(list)
        for src, targets in self._direct_edges.items():
            for t in targets:
                reverse[t].append(src)
        for src, conds in self._cond_edges.items():
            for ce in conds:
                for t in self._all_conditional_targets(ce):
                    reverse[t].append(src)

        visited: set[str] = set()
        queue = [END]
        while queue:
            current = queue.pop(0)
            for src in reverse.get(current, []):
                if src != START and src not in visited:
                    visited.add(src)
                    queue.append(src)
        return visited

    def _all_conditional_targets(self, ce: _ConditionalEdgeDef) -> list[str]:
        """Extract all possible target node names from a conditional edge.

        When no mapping is provided, the router can return any node name
        (including END), so we conservatively include all graph nodes + END
        as potential targets.
        """
        targets: list[str] = []
        if ce.mapping:
            targets.extend(ce.mapping.values())
        else:
            # Without mapping, router returns node names directly.
            # All nodes + END are potential targets.
            targets.extend(self._nodes.keys())
            targets.append(END)
        if ce.then:
            targets.append(ce.then)
        return targets

    # ── Public execution API ───────────────────────────────────────

    async def invoke(
        self,
        input_state: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run graph to completion. Returns the final state dict.

        Args:
            input_state: Initial state (merged with schema defaults).
            config: Optional ``{"configurable": {"thread_id": "..."}}``.
        """
        state = await self._init_state(input_state, config)
        thread_id = self._get_thread_id(config)
        step = 0

        # Resolve entry nodes from START
        next_nodes = self._resolve_next(START, state)

        while next_nodes:
            if step >= self._recursion_limit:
                raise GraphRuntimeError(
                    f"Recursion limit ({self._recursion_limit}) exceeded. "
                    f"Stuck at nodes: {next_nodes}"
                )

            # Execute current batch (may fan-out)
            state, executed = await self._execute_batch(next_nodes, state, thread_id, step)
            step += 1

            # Resolve next nodes from all executed nodes
            all_next: list[str] = []
            for node_name in executed:
                all_next.extend(self._resolve_next(node_name, state))

            # Deduplicate while preserving order
            seen: set[str] = set()
            next_nodes = []
            for n in all_next:
                if n == END:
                    continue
                if n not in seen:
                    seen.add(n)
                    next_nodes.append(n)

            # If any branch reached END and no more nodes, we're done
            if not next_nodes and END in all_next:
                break

        # Final checkpoint
        if self._checkpointer and thread_id:
            await self._checkpointer.save(thread_id, state, END, step)

        return state

    async def stream(
        self,
        input_state: dict[str, Any] | None = None,
        config: dict[str, Any] | None = None,
        *,
        stream_mode: StreamMode | str = StreamMode.VALUES,
    ) -> AsyncGenerator[GraphStreamEvent, None]:
        """Run graph, yielding events per node execution.

        Args:
            input_state: Initial state.
            config: Optional config with thread_id.
            stream_mode: What to yield — "values", "updates", or "debug".
        """
        if isinstance(stream_mode, str):
            stream_mode = StreamMode(stream_mode)

        state = await self._init_state(input_state, config)
        thread_id = self._get_thread_id(config)
        step = 0

        next_nodes = self._resolve_next(START, state)

        while next_nodes:
            if step >= self._recursion_limit:
                raise GraphRuntimeError(
                    f"Recursion limit ({self._recursion_limit}) exceeded"
                )

            # Execute and yield per node
            for node_name in next_nodes:
                t0 = time.monotonic()
                update = await self._invoke_node(node_name, state)
                duration_ms = (time.monotonic() - t0) * 1000
                state = apply_update(state, update, self._reducers)

                # Checkpoint
                if self._checkpointer and thread_id:
                    await self._checkpointer.save(thread_id, state, node_name, step)

                # Yield event
                if stream_mode == StreamMode.VALUES:
                    yield GraphStreamEvent(node_name, dict(state), stream_mode)
                elif stream_mode == StreamMode.UPDATES:
                    yield GraphStreamEvent(node_name, update, stream_mode)
                elif stream_mode == StreamMode.DEBUG:
                    yield GraphStreamEvent(
                        node_name,
                        {"state": dict(state), "update": update, "duration_ms": duration_ms},
                        stream_mode,
                    )

            step += 1

            # Resolve next
            all_next: list[str] = []
            for node_name in next_nodes:
                all_next.extend(self._resolve_next(node_name, state))

            seen: set[str] = set()
            next_nodes = []
            for n in all_next:
                if n == END:
                    continue
                if n not in seen:
                    seen.add(n)
                    next_nodes.append(n)

            if not next_nodes and END in all_next:
                break

        # Final checkpoint
        if self._checkpointer and thread_id:
            await self._checkpointer.save(thread_id, state, END, step)

    async def abatch(
        self,
        inputs: list[dict[str, Any]],
        configs: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run multiple inputs concurrently.

        Args:
            inputs: List of input states.
            configs: Optional per-input configs (must be same length as inputs).
        """
        configs = configs or [None] * len(inputs)  # type: ignore[list-item]
        tasks = [
            self.invoke(inp, cfg)
            for inp, cfg in zip(inputs, configs)
        ]
        return await asyncio.gather(*tasks)

    # ── Introspection ──────────────────────────────────────────────

    def get_graph_structure(self) -> dict[str, Any]:
        """Return a serializable representation of the graph topology."""
        nodes = list(self._nodes.keys())
        edges = [
            {"source": e.source, "target": e.target}
            for e in self._edges
        ]
        conditional = [
            {
                "source": ce.source,
                "targets": self._all_conditional_targets(ce),
                "mapping": ce.mapping,
            }
            for ce in self._conditional_edges
        ]
        return {
            "name": self.name,
            "nodes": nodes,
            "edges": edges,
            "conditional_edges": conditional,
        }

    # ── Internal execution ─────────────────────────────────────────

    async def _init_state(
        self, input_state: dict[str, Any] | None, config: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Build initial state: schema defaults ← checkpointed state ← input."""
        state = get_default_state(self._state_schema)

        # Resume from checkpoint if thread_id exists
        thread_id = self._get_thread_id(config)
        if self._checkpointer and thread_id:
            saved = await self._checkpointer.load(thread_id)
            if saved:
                state = apply_update(state, saved, self._reducers)

        # Overlay input
        if input_state:
            state = apply_update(state, input_state, self._reducers)

        return state

    async def _execute_batch(
        self,
        node_names: list[str],
        state: dict[str, Any],
        thread_id: str | None,
        step: int,
    ) -> tuple[dict[str, Any], list[str]]:
        """Execute a batch of nodes (fan-out) and merge results."""
        if len(node_names) == 1:
            # Fast path — no concurrency overhead
            name = node_names[0]
            update = await self._invoke_node(name, state)
            state = apply_update(state, update, self._reducers)
            if self._checkpointer and thread_id:
                await self._checkpointer.save(thread_id, state, name, step)
            return state, [name]

        # Fan-out: execute concurrently
        tasks = [self._invoke_node(name, state) for name in node_names]
        updates = await asyncio.gather(*tasks, return_exceptions=True)

        executed: list[str] = []
        for name, update in zip(node_names, updates):
            if isinstance(update, Exception):
                raise GraphRuntimeError(
                    f"Node '{name}' raised: {update}"
                ) from update
            state = apply_update(state, update, self._reducers)
            executed.append(name)

        if self._checkpointer and thread_id:
            await self._checkpointer.save(thread_id, state, f"batch:{','.join(executed)}", step)

        return state, executed

    async def _invoke_node(self, name: str, state: dict[str, Any]) -> dict[str, Any]:
        """Invoke a single node function with the current state."""
        node_def = self._nodes[name]
        # Pass a copy to prevent node from mutating shared state
        state_copy = copy.deepcopy(state)

        try:
            if node_def.is_async:
                result = await node_def.fn(state_copy)
            else:
                result = node_def.fn(state_copy)
        except Exception as exc:
            raise GraphRuntimeError(
                f"Node '{name}' failed: {exc}"
            ) from exc

        # Validate result
        if result is None:
            return {}
        if not isinstance(result, dict):
            raise InvalidStateUpdateError(
                f"Node '{name}' must return dict or None, got {type(result).__name__}"
            )
        return result

    def _resolve_next(self, source: str, state: dict[str, Any]) -> list[str]:
        """Determine the next node(s) to execute after *source*."""
        targets: list[str] = []

        # Direct edges
        targets.extend(self._direct_edges.get(source, []))

        # Conditional edges
        for ce in self._cond_edges.get(source, []):
            result = ce.router_fn(state)

            # Router may return a string or list of strings
            if isinstance(result, str):
                branch_keys = [result]
            elif isinstance(result, (list, tuple)):
                branch_keys = list(result)
            else:
                raise GraphRuntimeError(
                    f"Router from '{source}' returned invalid type: {type(result).__name__}. "
                    f"Expected str or list[str]."
                )

            for key in branch_keys:
                if ce.mapping:
                    if key not in ce.mapping:
                        raise GraphRuntimeError(
                            f"Router from '{source}' returned '{key}', "
                            f"not in mapping keys: {list(ce.mapping.keys())}"
                        )
                    targets.append(ce.mapping[key])
                else:
                    # No mapping — key IS the node name
                    targets.append(key)

            # "then" node appended after routed targets
            if ce.then:
                targets.append(ce.then)

        # Deduplicate preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for t in targets:
            if t not in seen:
                seen.add(t)
                unique.append(t)

        return unique

    @staticmethod
    def _get_thread_id(config: dict[str, Any] | None) -> str | None:
        if not config:
            return None
        configurable = config.get("configurable", {})
        return configurable.get("thread_id")

    def __repr__(self) -> str:
        n = len(self._nodes)
        e = len(self._edges) + len(self._conditional_edges)
        return f"CompiledGraph(name={self.name!r}, nodes={n}, edges={e})"

"""Pre-built node factories for common patterns.

Provides convenience wrappers that integrate the graph layer with
the agent framework's existing execution engine (``AgentFramework.run()``).

Usage::

    from agent_framework.graph.nodes import agent_node, tool_node

    graph.add_node("researcher", agent_node(framework, task_key="query"))
    graph.add_node("summarize", tool_node(my_summarize_fn))
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from agent_framework.entry import AgentFramework


def agent_node(
    framework: AgentFramework,
    *,
    task_key: str = "task",
    output_key: str = "result",
    user_id: str | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create a graph node that runs a full agent execution.

    The node reads ``state[task_key]`` as the agent task, runs
    ``framework.run(task)``, and writes the final answer to
    ``state[output_key]``.

    Args:
        framework: Configured ``AgentFramework`` instance.
        task_key: State key containing the task string.
        output_key: State key to write the agent's final answer.
        user_id: Optional user ID for memory scoping.

    Returns:
        An async node function ``(state) -> dict``.
    """

    async def _node(state: dict[str, Any]) -> dict[str, Any]:
        task = state.get(task_key, "")
        if not task:
            return {output_key: None}
        result = await framework.run(task, user_id=user_id)
        return {output_key: result.final_answer}

    _node.__name__ = f"agent_node({task_key}→{output_key})"
    return _node


def tool_node(
    fn: Callable[..., Any],
    *,
    input_key: str | None = None,
    output_key: str | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Wrap a plain function as a graph node.

    If *input_key* is set, passes ``state[input_key]`` as the sole argument.
    Otherwise passes the full state dict.

    If *output_key* is set, wraps the return value as ``{output_key: result}``.
    Otherwise expects the function to return a dict.

    Args:
        fn: Sync or async callable.
        input_key: Optional state key to extract as function argument.
        output_key: Optional state key for the return value.
    """
    is_async = asyncio.iscoroutinefunction(fn)

    async def _node(state: dict[str, Any]) -> dict[str, Any]:
        arg = state[input_key] if input_key else state
        if is_async:
            result = await fn(arg)
        else:
            result = fn(arg)
        if output_key:
            return {output_key: result}
        if isinstance(result, dict):
            return result
        return {}

    _node.__name__ = getattr(fn, "__name__", "tool_node")
    return _node


def passthrough_node(
    transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Create a node that optionally transforms state without side effects.

    If *transform* is ``None``, returns an empty update (no-op).
    """

    def _node(state: dict[str, Any]) -> dict[str, Any]:
        if transform:
            return transform(state)
        return {}

    _node.__name__ = "passthrough"
    return _node


def branch_node(
    condition: Callable[[dict[str, Any]], str],
) -> Callable[[dict[str, Any]], str]:
    """Create a router function for ``add_conditional_edges``.

    Wraps a condition function to ensure it always returns a string.
    Purely for readability / documentation purposes.
    """
    return condition

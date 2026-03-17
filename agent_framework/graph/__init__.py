"""agent_framework.graph — LangGraph-compatible compiled graph execution.

Quick start::

    import operator
    from typing import Annotated
    from typing_extensions import TypedDict
    from agent_framework.graph import StateGraph, START, END

    class State(TypedDict):
        messages: Annotated[list[str], operator.add]
        count: int

    def greet(state: State) -> dict:
        return {"messages": ["Hello!"], "count": state["count"] + 1}

    graph = StateGraph(State)
    graph.add_node("greet", greet)
    graph.add_edge(START, "greet")
    graph.add_edge("greet", END)
    app = graph.compile()
    result = await app.invoke({"messages": [], "count": 0})
    # {'messages': ['Hello!'], 'count': 1}
"""

from agent_framework.graph.compiled import (
    CheckpointerProtocol,
    CompiledGraph,
    GraphStreamEvent,
    InMemorySaver,
)
from agent_framework.graph.constants import END, START, StreamMode
from agent_framework.graph.errors import (
    DuplicateNodeError,
    GraphBuildError,
    GraphCompilationError,
    GraphError,
    GraphRuntimeError,
    InvalidEdgeError,
    InvalidStateUpdateError,
    NoPathToEndError,
    NodeNotFoundError,
    UnreachableNodeError,
)
from agent_framework.graph.graph import StateGraph
from agent_framework.graph.nodes import agent_node, branch_node, passthrough_node, tool_node
from agent_framework.graph.state import apply_update, extract_reducers

__all__ = [
    # Builder
    "StateGraph",
    # Compiled
    "CompiledGraph",
    "GraphStreamEvent",
    "InMemorySaver",
    "CheckpointerProtocol",
    # Constants
    "START",
    "END",
    "StreamMode",
    # Node helpers
    "agent_node",
    "tool_node",
    "passthrough_node",
    "branch_node",
    # State utilities
    "extract_reducers",
    "apply_update",
    # Errors
    "GraphError",
    "GraphBuildError",
    "GraphCompilationError",
    "GraphRuntimeError",
    "DuplicateNodeError",
    "NodeNotFoundError",
    "InvalidEdgeError",
    "UnreachableNodeError",
    "NoPathToEndError",
    "InvalidStateUpdateError",
]

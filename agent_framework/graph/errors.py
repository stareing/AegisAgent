"""Graph-specific exceptions.

All graph errors inherit from GraphError so callers can catch
the entire family with a single except clause.
"""

from __future__ import annotations


class GraphError(Exception):
    """Base class for all graph-related errors."""


class GraphBuildError(GraphError):
    """Raised when the graph definition is invalid (missing edges, unreachable nodes, etc.)."""


class GraphCompilationError(GraphError):
    """Raised when compile() detects structural problems."""


class GraphRuntimeError(GraphError):
    """Raised during graph execution (node failure, invalid state update, etc.)."""


class NodeNotFoundError(GraphBuildError):
    """Raised when referencing a node that hasn't been added."""

    def __init__(self, node_name: str) -> None:
        self.node_name = node_name
        super().__init__(f"Node '{node_name}' not found in graph")


class DuplicateNodeError(GraphBuildError):
    """Raised when adding a node with an existing name."""

    def __init__(self, node_name: str) -> None:
        self.node_name = node_name
        super().__init__(f"Node '{node_name}' already exists in graph")


class InvalidEdgeError(GraphBuildError):
    """Raised when an edge references an invalid node."""


class UnreachableNodeError(GraphCompilationError):
    """Raised when a node cannot be reached from START."""

    def __init__(self, nodes: set[str]) -> None:
        self.nodes = nodes
        super().__init__(f"Unreachable nodes: {nodes}")


class NoPathToEndError(GraphCompilationError):
    """Raised when some nodes have no path to END."""

    def __init__(self, nodes: set[str]) -> None:
        self.nodes = nodes
        super().__init__(f"Nodes with no path to END: {nodes}")


class InvalidStateUpdateError(GraphRuntimeError):
    """Raised when a node returns an invalid state update."""

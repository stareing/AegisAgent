"""Tool concurrency partitioning — concurrent-safe vs serial batching.

Classifies tools as concurrent-safe (reads) or non-concurrent (writes).
Concurrent-safe tools in a batch run in parallel via asyncio.gather.
Non-concurrent tools run serially.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_framework.models.message import ToolCallRequest
    from agent_framework.protocols.core import ToolRegistryProtocol

# Default concurrency class when a tool is not found in the registry.
# Conservative: unknown tools run serially.
_DEFAULT_CLASS = "non_concurrent"


class ConcurrencyClass(str, Enum):
    """Whether a tool may safely execute concurrently with others."""

    CONCURRENT_SAFE = "concurrent_safe"
    NON_CONCURRENT = "non_concurrent"


@dataclass(frozen=True)
class ToolCallBatch:
    """A contiguous group of tool-call requests sharing the same concurrency class."""

    requests: list[ToolCallRequest] = field(default_factory=list)
    concurrent: bool = False


class ConcurrencyPartitioner:
    """Partition a list of tool-call requests into concurrent / serial batches.

    Consecutive requests that share the same :class:`ConcurrencyClass` are
    grouped together.  The resulting list of :class:`ToolCallBatch` objects
    preserves the original ordering so that downstream commit sequencing
    remains deterministic.
    """

    @staticmethod
    def partition(
        requests: list[ToolCallRequest],
        registry: ToolRegistryProtocol,
    ) -> list[ToolCallBatch]:
        """Group *requests* into batches by concurrency class.

        Args:
            requests: Ordered tool-call requests from a single iteration.
            registry: Used to look up ``ToolMeta.concurrency_class`` per tool.

        Returns:
            A list of ``ToolCallBatch`` preserving the original request order.
        """
        if not requests:
            return []

        batches: list[ToolCallBatch] = []
        current_class: ConcurrencyClass | None = None
        current_items: list[ToolCallRequest] = []

        for req in requests:
            cc = _resolve_class(req.function_name, registry)
            if cc != current_class:
                # Flush accumulated batch
                if current_items:
                    batches.append(
                        ToolCallBatch(
                            requests=current_items,
                            concurrent=(current_class == ConcurrencyClass.CONCURRENT_SAFE),
                        )
                    )
                current_class = cc
                current_items = [req]
            else:
                current_items.append(req)

        # Flush last batch
        if current_items:
            batches.append(
                ToolCallBatch(
                    requests=current_items,
                    concurrent=(current_class == ConcurrencyClass.CONCURRENT_SAFE),
                )
            )

        return batches


def _resolve_class(
    tool_name: str,
    registry: ToolRegistryProtocol,
) -> ConcurrencyClass:
    """Look up the concurrency class for *tool_name* from the registry."""
    if not registry.has_tool(tool_name):
        return ConcurrencyClass(_DEFAULT_CLASS)

    entry = registry.get_tool(tool_name)
    raw = getattr(entry.meta, "concurrency_class", _DEFAULT_CLASS)
    try:
        return ConcurrencyClass(raw)
    except ValueError:
        return ConcurrencyClass(_DEFAULT_CLASS)

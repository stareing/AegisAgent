from __future__ import annotations

import inspect
from typing import Callable

from agent_framework.infra.logger import get_logger
from agent_framework.models.tool import ToolEntry, ToolMeta

logger = get_logger(__name__)


class GlobalToolCatalog:
    """Process-level tool catalog. Not used directly by agents."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}

    def register(self, entry: ToolEntry) -> None:
        """Register a tool entry."""
        qualified = self._qualified_name(entry.meta)
        if qualified in self._tools:
            logger.warning("tool.overwritten", tool_name=qualified)
        self._tools[qualified] = entry
        logger.info("tool.registered", tool_name=qualified, source=entry.meta.source)

    def register_function(self, func: Callable) -> None:
        """Register a function decorated with @tool."""
        meta: ToolMeta | None = getattr(func, "__tool_meta__", None)
        if meta is None:
            raise ValueError(f"Function {func.__name__} is not decorated with @tool")
        validator = getattr(func, "__tool_validator__", None)
        entry = ToolEntry(meta=meta, callable_ref=func, validator_model=validator)
        self.register(entry)

    def register_module(self, module: object) -> int:
        """Register all @tool-decorated functions from a module."""
        count = 0
        for _name, obj in inspect.getmembers(module, inspect.isfunction):
            if hasattr(obj, "__tool_meta__"):
                self.register_function(obj)
                count += 1
        return count

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> ToolEntry | None:
        return self._tools.get(name)

    def list_all(self) -> list[ToolEntry]:
        return list(self._tools.values())

    def _qualified_name(self, meta: ToolMeta) -> str:
        """Build qualified name based on source."""
        if meta.source == "local":
            return f"local::{meta.name}"
        if meta.source == "mcp":
            return f"mcp::{meta.mcp_server_id}::{meta.name}"
        if meta.source == "a2a":
            alias = (meta.a2a_agent_url or "unknown").split("/")[-1]
            return f"a2a::{alias}::{meta.name}"
        if meta.source == "subagent":
            return f"subagent::{meta.name}"
        return meta.name

from __future__ import annotations

from agent_framework.models.tool import ToolEntry


def _qualified_name(meta) -> str:
    """Build qualified name per section 10.3 naming convention."""
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


class ToolRegistry:
    """Runtime tool registry for an agent instance.

    Stores tools by qualified name (e.g. local::read_file, mcp::server1::search).
    Lookup supports both qualified and bare name.
    """

    def __init__(self) -> None:
        self._tools: dict[str, ToolEntry] = {}
        self._bare_to_qualified: dict[str, str] = {}

    def register(self, entry: ToolEntry) -> None:
        qname = _qualified_name(entry.meta)
        self._tools[qname] = entry
        self._bare_to_qualified[entry.meta.name] = qname
        # Register aliases so they resolve to the same qualified name
        for alias in entry.meta.aliases:
            self._bare_to_qualified[alias] = qname

    # Alias for backward compatibility
    add = register

    def remove(self, name: str) -> bool:
        qname = self._resolve(name)
        if qname and qname in self._tools:
            entry = self._tools.pop(qname)
            self._bare_to_qualified.pop(entry.meta.name, None)
            for alias in entry.meta.aliases:
                self._bare_to_qualified.pop(alias, None)
            return True
        return False

    def get_tool(self, name: str) -> ToolEntry:
        qname = self._resolve(name)
        if qname is None:
            raise KeyError(f"Tool not found: {name}")
        return self._tools[qname]

    def has_tool(self, name: str) -> bool:
        return self._resolve(name) is not None

    def list_tools(
        self,
        category: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> list[ToolEntry]:
        result = list(self._tools.values())
        if category:
            result = [e for e in result if e.meta.category == category]
        if tags:
            tag_set = set(tags)
            result = [e for e in result if tag_set & set(e.meta.tags)]
        if source:
            result = [e for e in result if e.meta.source == source]
        return result

    def export_schemas(
        self,
        whitelist: list[str] | None = None,
        include_deferred: bool = False,
    ) -> list[dict]:
        """Export tool schemas in OpenAI function-calling format.

        Uses bare name in schema for LLM consumption.
        Deferred tools (should_defer=True) are excluded by default unless
        include_deferred is True or they appear in the whitelist.
        """
        entries = list(self._tools.values())
        if whitelist is not None:
            wl_set = set(whitelist)
            entries = [e for e in entries if e.meta.name in wl_set]
        elif not include_deferred:
            # v4.1: always_load tools are included even when deferred
            entries = [
                e for e in entries
                if not e.meta.should_defer or e.meta.always_load
            ]

        schemas = []
        for entry in entries:
            description = entry.meta.description
            if entry.meta.prompt:
                description = f"{description}\n\n{entry.meta.prompt}" if description else entry.meta.prompt
            schema: dict = {
                "type": "function",
                "function": {
                    "name": entry.meta.name,
                    "description": description,
                },
            }
            if entry.meta.parameters_schema:
                schema["function"]["parameters"] = entry.meta.parameters_schema
            schemas.append(schema)
        return schemas

    def snapshot(self) -> dict[str, ToolEntry]:
        return dict(self._tools)

    def _resolve(self, name: str) -> str | None:
        """Resolve bare or qualified name to qualified name."""
        if name in self._tools:
            return name
        return self._bare_to_qualified.get(name)


class ScopedToolRegistry:
    """Read-only view of a ToolRegistry filtered by whitelist.

    IMPORTANT — visibility vs execution boundary:
    ScopedToolRegistry provides VISIBILITY FILTERING only. It controls which
    tools appear in export_schemas() (what the LLM can see), but it is NOT
    a security boundary.

    The true execution security boundary is ToolExecutor.is_tool_allowed()
    which re-checks CapabilityPolicy at execution time. Even if a tool is
    invisible in the scoped registry, a crafted tool_call name could bypass
    this layer. Therefore:
    - ScopedToolRegistry → visibility optimisation (what the LLM sees)
    - ToolExecutor.is_tool_allowed() → security enforcement (what can run)

    Whitelist matches against bare tool names.
    """

    def __init__(self, source: ToolRegistry, whitelist: list[str] | None = None) -> None:
        snapshot = source.snapshot()
        if whitelist is not None:
            wl_set = set(whitelist)
            self._tools = {
                n: e for n, e in snapshot.items() if e.meta.name in wl_set
            }
        else:
            self._tools = snapshot
        # Build bare name lookup
        self._bare_to_qualified: dict[str, str] = {}
        for qname, entry in self._tools.items():
            self._bare_to_qualified[entry.meta.name] = qname

    def get_tool(self, name: str) -> ToolEntry:
        qname = self._resolve(name)
        if qname is None:
            raise KeyError(f"Tool not found in scope: {name}")
        return self._tools[qname]

    def has_tool(self, name: str) -> bool:
        return self._resolve(name) is not None

    def list_tools(
        self,
        category: str | None = None,
        tags: list[str] | None = None,
        source: str | None = None,
    ) -> list[ToolEntry]:
        result = list(self._tools.values())
        if category:
            result = [e for e in result if e.meta.category == category]
        if tags:
            tag_set = set(tags)
            result = [e for e in result if tag_set & set(e.meta.tags)]
        if source:
            result = [e for e in result if e.meta.source == source]
        return result

    def export_schemas(self, whitelist: list[str] | None = None) -> list[dict]:
        entries = list(self._tools.values())
        if whitelist is not None:
            wl_set = set(whitelist)
            entries = [e for e in entries if e.meta.name in wl_set]

        schemas = []
        for entry in entries:
            # v4.1: Append prompt to description (same as main ToolRegistry)
            description = entry.meta.description
            if entry.meta.prompt:
                description = f"{description}\n\n{entry.meta.prompt}" if description else entry.meta.prompt
            schema: dict = {
                "type": "function",
                "function": {
                    "name": entry.meta.name,
                    "description": description,
                },
            }
            if entry.meta.parameters_schema:
                schema["function"]["parameters"] = entry.meta.parameters_schema
            schemas.append(schema)
        return schemas

    def _resolve(self, name: str) -> str | None:
        if name in self._tools:
            return name
        return self._bare_to_qualified.get(name)

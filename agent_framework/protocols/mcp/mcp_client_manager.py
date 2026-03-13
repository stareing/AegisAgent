from __future__ import annotations

import asyncio
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.mcp import MCPServerConfig, MCPToolInfo, MCPTransportType
from agent_framework.models.tool import ToolEntry, ToolMeta

logger = get_logger(__name__)


class MCPClientManager:
    """Manages connections to MCP servers and syncs discovered tools.

    Responsibilities:
    - Connect to MCP servers (stdio / sse / streamable_http)
    - Discover tools from each server
    - Sync discovered tools into the GlobalToolCatalog
    - Route tool calls to the correct MCP server
    """

    def __init__(self) -> None:
        self._servers: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, Any] = {}  # server_id -> MCP client session
        self._discovered_tools: dict[str, list[MCPToolInfo]] = {}

    async def connect_server(self, config: MCPServerConfig) -> None:
        """Connect to an MCP server and discover its tools."""
        server_id = config.server_id
        self._servers[server_id] = config

        try:
            client, session = await self._create_client(config)
            self._clients[server_id] = session

            # Discover tools
            tools = await self._discover_tools(session, server_id)
            self._discovered_tools[server_id] = tools

            logger.info(
                "mcp.server_connected",
                server_id=server_id,
                transport=config.transport.value,
                tools_count=len(tools),
            )
        except Exception as e:
            logger.error(
                "mcp.connect_failed",
                server_id=server_id,
                error=str(e),
            )
            raise

    async def disconnect_server(self, server_id: str) -> None:
        """Disconnect from an MCP server."""
        client = self._clients.pop(server_id, None)
        self._servers.pop(server_id, None)
        self._discovered_tools.pop(server_id, None)
        if client and hasattr(client, "close"):
            try:
                await client.close()
            except Exception:
                pass
        logger.info("mcp.server_disconnected", server_id=server_id)

    async def disconnect_all(self) -> None:
        """Disconnect from all MCP servers."""
        server_ids = list(self._clients.keys())
        for sid in server_ids:
            await self.disconnect_server(sid)

    def sync_tools_to_catalog(
        self, catalog: Any, server_id: str | None = None
    ) -> int:
        """Register discovered MCP tools into a GlobalToolCatalog.

        Returns the number of tools registered.
        """
        count = 0
        if server_id is not None:
            items = [(server_id, self._discovered_tools.get(server_id, []))]
        else:
            items = list(self._discovered_tools.items())

        for sid, tools in items:
            for tool_info in tools:
                meta = ToolMeta(
                    name=tool_info.name,
                    description=tool_info.description,
                    parameters_schema=tool_info.input_schema,
                    source="mcp",
                    mcp_server_id=sid,
                    is_async=True,
                )
                entry = ToolEntry(meta=meta, callable_ref=None, validator_model=None)
                catalog.register(entry)
                count += 1
        logger.info("mcp.tools_synced", total=count)
        return count

    async def call_mcp_tool(
        self, server_id: str, tool_name: str, arguments: dict
    ) -> Any:
        """Call a tool on a specific MCP server."""
        session = self._clients.get(server_id)
        if session is None:
            raise RuntimeError(f"MCP server '{server_id}' not connected")

        try:
            result = await session.call_tool(tool_name, arguments=arguments)
            logger.info(
                "mcp.tool_called",
                server_id=server_id,
                tool_name=tool_name,
            )
            # Extract text content from MCP result
            if hasattr(result, "content") and result.content:
                parts = []
                for block in result.content:
                    if hasattr(block, "text"):
                        parts.append(block.text)
                    else:
                        parts.append(str(block))
                return "\n".join(parts)
            return str(result)
        except Exception as e:
            logger.error(
                "mcp.tool_call_failed",
                server_id=server_id,
                tool_name=tool_name,
                error=str(e),
            )
            raise

    def get_discovered_tools(self, server_id: str | None = None) -> list[MCPToolInfo]:
        """Get discovered tools, optionally filtered by server."""
        if server_id:
            return self._discovered_tools.get(server_id, [])
        all_tools: list[MCPToolInfo] = []
        for tools in self._discovered_tools.values():
            all_tools.extend(tools)
        return all_tools

    def list_connected_servers(self) -> list[str]:
        return list(self._clients.keys())

    @staticmethod
    def load_config_file(path: str) -> list[MCPServerConfig]:
        """Load MCP server configurations from a JSON file.

        Expected format: {"mcpServers": {"server_id": {...config...}, ...}}
        """
        import json
        from pathlib import Path

        config_path = Path(path)
        if not config_path.exists():
            logger.warning("mcp.config_not_found", path=path)
            return []

        with open(config_path) as f:
            data = json.load(f)

        configs: list[MCPServerConfig] = []
        servers = data.get("mcpServers", data.get("servers", {}))
        for server_id, server_data in servers.items():
            server_data["server_id"] = server_id
            configs.append(MCPServerConfig(**server_data))

        logger.info("mcp.config_loaded", path=path, servers_count=len(configs))
        return configs

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _create_client(self, config: MCPServerConfig) -> tuple[Any, Any]:
        """Create an MCP client connection based on transport type.

        Returns (context_manager, session).
        Uses the official `mcp` Python SDK.
        """
        try:
            from mcp import ClientSession
        except ImportError:
            raise ImportError(
                "MCP SDK not installed. Install with: pip install mcp"
            )

        if config.transport == MCPTransportType.STDIO:
            from mcp.client.stdio import stdio_client, StdioServerParameters

            if not config.command:
                raise ValueError(f"STDIO transport requires 'command' for server {config.server_id}")

            server_params = StdioServerParameters(
                command=config.command,
                args=config.args,
                env=config.env if config.env else None,
            )
            # stdio_client is an async context manager that yields (read, write)
            transport = stdio_client(server_params)
            read_stream, write_stream = await transport.__aenter__()
            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            await session.initialize()
            # Store transport for cleanup
            session._transport_cm = transport  # type: ignore[attr-defined]
            return transport, session

        if config.transport == MCPTransportType.SSE:
            from mcp.client.sse import sse_client

            if not config.url:
                raise ValueError(f"SSE transport requires 'url' for server {config.server_id}")

            transport = sse_client(config.url, headers=config.headers or None)
            read_stream, write_stream = await transport.__aenter__()
            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            await session.initialize()
            session._transport_cm = transport  # type: ignore[attr-defined]
            return transport, session

        if config.transport == MCPTransportType.STREAMABLE_HTTP:
            # streamable_http uses the same pattern as SSE in newer SDK versions
            try:
                from mcp.client.streamable_http import streamablehttp_client
            except ImportError:
                from mcp.client.sse import sse_client as streamablehttp_client

            if not config.url:
                raise ValueError(f"Streamable HTTP transport requires 'url' for server {config.server_id}")

            transport = streamablehttp_client(config.url, headers=config.headers or None)
            read_stream, write_stream = await transport.__aenter__()
            session = ClientSession(read_stream, write_stream)
            await session.__aenter__()
            await session.initialize()
            session._transport_cm = transport  # type: ignore[attr-defined]
            return transport, session

        raise ValueError(f"Unsupported transport type: {config.transport}")

    async def _discover_tools(self, session: Any, server_id: str) -> list[MCPToolInfo]:
        """Discover tools from an MCP server session."""
        result = await session.list_tools()
        tools: list[MCPToolInfo] = []
        for tool in result.tools:
            tools.append(
                MCPToolInfo(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema if hasattr(tool, "inputSchema") else {},
                    server_id=server_id,
                )
            )
        return tools

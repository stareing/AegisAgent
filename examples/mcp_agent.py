"""Example: Agent with MCP server integration.

Demonstrates how to connect to MCP servers and use their tools.
"""

import asyncio

from agent_framework.entry import AgentFramework
from agent_framework.models.mcp import MCPServerConfig, MCPTransportType


async def main():
    # Create framework with MCP config
    framework = AgentFramework()
    framework.setup(auto_approve_tools=True)

    # Option 1: Connect to an MCP server programmatically
    config = MCPServerConfig(
        server_id="my_mcp_server",
        transport=MCPTransportType.STDIO,
        command="node",
        args=["path/to/mcp-server.js"],
    )

    # Initialize MCP (requires `pip install mcp`)
    try:
        from agent_framework.protocols.mcp.mcp_client_manager import \
            MCPClientManager

        mcp = MCPClientManager()
        await mcp.connect_server(config)

        # Sync discovered tools to framework
        mcp.sync_tools_to_catalog(framework._catalog)
        print(f"Connected! Discovered tools: {[t.name for t in mcp.get_discovered_tools()]}")

        # Option 2: Load from config file
        # configs = MCPClientManager.load_config_file("mcp_config.json")
        # for cfg in configs:
        #     await mcp.connect_server(cfg)

        # Run the agent — it can now use MCP tools
        result = await framework.run("Use the available tools to help me.")
        print(f"Answer: {result.final_answer}")

        await mcp.disconnect_all()
    except ImportError:
        print("MCP SDK not installed. Install with: pip install mcp")

    await framework.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

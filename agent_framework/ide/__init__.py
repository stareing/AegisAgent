"""IDE companion — MCP server exposing framework capabilities to IDEs.

Provides an MCP (Model Context Protocol) server that allows VS Code,
JetBrains, and other IDE clients to interact with the agent framework:

- Run agent tasks from the IDE
- List and invoke registered tools
- Query and manage memories
- Stream execution events in real-time
- Get context-aware code suggestions

Usage:
    # Start the MCP server (stdio transport)
    python -m agent_framework.ide.server

    # Or programmatically
    from agent_framework.ide.server import create_mcp_server
    server = create_mcp_server(config_path="config/deepseek.json")
    await server.run_stdio()
"""

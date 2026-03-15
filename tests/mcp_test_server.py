"""Minimal MCP test server for integration testing.

Run: python tests/mcp_test_server.py
Exposes two tools via stdio transport.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("test-server")


@mcp.tool()
def echo(message: str) -> str:
    """Echo back the given message."""
    return f"echo: {message}"


@mcp.tool()
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)


if __name__ == "__main__":
    mcp.run(transport="stdio")

"""MCP server exposing agent framework capabilities to IDE clients.

Implements the Model Context Protocol (MCP) server specification,
providing tools and resources that IDE extensions can consume.

Exposed MCP tools:
  - agent_run: Execute an agent task and return the result
  - agent_run_stream: Execute with streaming events (SSE-compatible)
  - list_tools: List all registered tools with schemas
  - list_skills: List available skills with descriptions
  - list_memories: Query stored memories
  - save_memory: Store a new memory
  - get_file_context: Get file context for a given path
  - search_codebase: Search codebase with grep/glob

Exposed MCP resources:
  - agent://status: Current agent status and configuration
  - agent://tools: Tool catalog as JSON
  - agent://memories: Memory store contents

Transport: stdio (default) or SSE for web-based IDEs.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# MCP protocol version
MCP_PROTOCOL_VERSION = "2024-11-05"


class MCPIDEServer:
    """MCP server that exposes agent framework to IDE clients.

    Design:
    - Lazy framework initialization (only when first tool is called)
    - Stateless request handling (each request is independent)
    - Framework instance is shared across all requests
    """

    def __init__(
        self,
        config_path: str | None = None,
        workspace_dir: str | None = None,
    ) -> None:
        self._config_path = config_path
        self._workspace_dir = workspace_dir or os.getcwd()
        self._framework: Any = None
        self._setup_done = False

    def _ensure_framework(self) -> Any:
        """Lazy-initialize the agent framework."""
        if self._setup_done:
            return self._framework

        from agent_framework.entry import AgentFramework

        fw = AgentFramework(config_path=self._config_path)
        fw.setup(auto_approve_tools=True)
        self._framework = fw
        self._setup_done = True
        logger.info("ide_server.framework_initialized")
        return fw

    # ── MCP Protocol Implementation ──────────────────────────────

    def get_server_info(self) -> dict[str, Any]:
        """Return MCP server capabilities."""
        return {
            "name": "agent-framework-ide",
            "version": "1.0.0",
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": False, "listChanged": False},
            },
        }

    def list_mcp_tools(self) -> list[dict[str, Any]]:
        """Return MCP tool definitions."""
        return [
            {
                "name": "agent_run",
                "description": "Execute an agent task. Returns the final answer and metadata.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The task or question for the agent",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "description": "Timeout in milliseconds (default: 300000)",
                            "default": 300000,
                        },
                    },
                    "required": ["task"],
                },
            },
            {
                "name": "list_tools",
                "description": "List all registered tools with their schemas and descriptions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "Filter by tool category (optional)",
                        },
                    },
                },
            },
            {
                "name": "list_skills",
                "description": "List all available agent skills with descriptions.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "list_memories",
                "description": "Query stored memories. Returns memory entries.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Search query (optional)",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max entries to return",
                            "default": 20,
                        },
                    },
                },
            },
            {
                "name": "save_memory",
                "description": "Store a new memory entry.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Memory content",
                        },
                        "kind": {
                            "type": "string",
                            "description": "Memory kind (preference, constraint, context)",
                            "default": "context",
                        },
                        "tags": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Tags for categorization",
                        },
                    },
                    "required": ["content"],
                },
            },
            {
                "name": "search_codebase",
                "description": "Search the codebase using grep pattern matching.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Search pattern (regex)",
                        },
                        "path": {
                            "type": "string",
                            "description": "Directory to search in (default: workspace root)",
                        },
                        "file_pattern": {
                            "type": "string",
                            "description": "Glob pattern for files (e.g. '*.py')",
                        },
                    },
                    "required": ["pattern"],
                },
            },
            {
                "name": "get_agent_status",
                "description": "Get current agent configuration and status.",
                "inputSchema": {"type": "object", "properties": {}},
            },
        ]

    def list_mcp_resources(self) -> list[dict[str, Any]]:
        """Return MCP resource definitions."""
        return [
            {
                "uri": "agent://status",
                "name": "Agent Status",
                "description": "Current agent configuration and runtime status",
                "mimeType": "application/json",
            },
            {
                "uri": "agent://tools",
                "name": "Tool Catalog",
                "description": "All registered tools with schemas",
                "mimeType": "application/json",
            },
            {
                "uri": "agent://memories",
                "name": "Memory Store",
                "description": "Stored memory entries",
                "mimeType": "application/json",
            },
        ]

    # ── Tool Handlers ────────────────────────────────────────────

    async def handle_tool_call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch an MCP tool call to the appropriate handler."""
        handlers = {
            "agent_run": self._handle_agent_run,
            "list_tools": self._handle_list_tools,
            "list_skills": self._handle_list_skills,
            "list_memories": self._handle_list_memories,
            "save_memory": self._handle_save_memory,
            "search_codebase": self._handle_search_codebase,
            "get_agent_status": self._handle_get_status,
        }

        handler = handlers.get(tool_name)
        if handler is None:
            return {"error": f"Unknown tool: {tool_name}"}

        try:
            return await handler(arguments)
        except Exception as e:
            logger.error("ide_server.tool_error", tool=tool_name, error=str(e))
            return {"error": str(e), "error_type": type(e).__name__}

    async def _handle_agent_run(self, args: dict[str, Any]) -> dict[str, Any]:
        """Execute an agent task."""
        fw = self._ensure_framework()
        task = args.get("task", "")
        timeout_ms = args.get("timeout_ms", 300000)

        result = await fw.run(task, run_timeout_ms=timeout_ms)
        return {
            "success": result.success,
            "final_answer": result.final_answer,
            "iterations_used": result.iterations_used,
            "total_tokens": result.usage.total_tokens if result.usage else 0,
            "run_id": result.run_id,
            "error": result.error,
        }

    async def _handle_list_tools(self, args: dict[str, Any]) -> dict[str, Any]:
        """List registered tools."""
        fw = self._ensure_framework()
        category = args.get("category")

        if fw._registry is None:
            return {"tools": []}

        tools = fw._registry.list_tools(category=category)
        return {
            "tools": [
                {
                    "name": t.meta.name,
                    "description": t.meta.description,
                    "category": t.meta.category,
                    "source": t.meta.source,
                    "require_confirm": t.meta.require_confirm,
                }
                for t in tools
            ]
        }

    async def _handle_list_skills(self, args: dict[str, Any]) -> dict[str, Any]:
        """List available skills."""
        fw = self._ensure_framework()
        if fw._deps is None or not hasattr(fw._deps, "skill_router"):
            return {"skills": []}

        skills = fw._deps.skill_router.list_skills()
        return {
            "skills": [
                {
                    "skill_id": s.skill_id,
                    "name": s.name,
                    "description": s.description,
                    "trigger_keywords": s.trigger_keywords,
                }
                for s in skills
            ]
        }

    async def _handle_list_memories(self, args: dict[str, Any]) -> dict[str, Any]:
        """Query memories."""
        fw = self._ensure_framework()
        memories = await fw.list_memories()
        return {"memories": memories or []}

    async def _handle_save_memory(self, args: dict[str, Any]) -> dict[str, Any]:
        """Store a memory."""
        fw = self._ensure_framework()
        content = args.get("content", "")
        kind = args.get("kind", "context")
        tags = args.get("tags", [])

        if hasattr(fw, "remember"):
            result = await fw.remember(content, kind=kind, tags=tags)
            return {"success": True, "memory_id": str(result) if result else ""}

        return {"success": False, "error": "Memory management not available"}

    async def _handle_search_codebase(self, args: dict[str, Any]) -> dict[str, Any]:
        """Search codebase with grep."""
        import subprocess

        pattern = args.get("pattern", "")
        path = args.get("path", self._workspace_dir)
        file_pattern = args.get("file_pattern", "")

        cmd = ["grep", "-rn", "--max-count=50"]
        if file_pattern:
            cmd.extend(["--include", file_pattern])
        cmd.extend([pattern, path])

        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=10,
            )
            lines = proc.stdout.strip().split("\n")[:50]
            return {
                "matches": [line for line in lines if line],
                "count": len([line for line in lines if line]),
            }
        except subprocess.TimeoutExpired:
            return {"error": "Search timed out", "matches": []}
        except FileNotFoundError:
            return {"error": "grep not found", "matches": []}

    async def _handle_get_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Get agent status and configuration."""
        fw = self._ensure_framework()
        config = fw.config

        tool_count = 0
        if fw._registry:
            tool_count = len(fw._registry.list_tools())

        return {
            "status": "ready",
            "model": {
                "adapter_type": config.model.adapter_type,
                "model_name": config.model.default_model_name,
            },
            "tools_registered": tool_count,
            "memory_store": config.memory.store_type,
            "approval_mode": config.tools.approval_mode,
            "workspace_dir": self._workspace_dir,
        }

    # ── Resource Handlers ────────────────────────────────────────

    async def handle_resource_read(self, uri: str) -> dict[str, Any]:
        """Read an MCP resource."""
        if uri == "agent://status":
            return await self._handle_get_status({})
        elif uri == "agent://tools":
            return await self._handle_list_tools({})
        elif uri == "agent://memories":
            return await self._handle_list_memories({})
        return {"error": f"Unknown resource: {uri}"}

    # ── stdio Transport ──────────────────────────────────────────

    async def run_stdio(self) -> None:
        """Run as an MCP server over stdio transport.

        Reads JSON-RPC requests from stdin, writes responses to stdout.
        Compatible with VS Code MCP client extension.
        """
        logger.info("ide_server.stdio_started")

        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await asyncio.get_event_loop().connect_read_pipe(
            lambda: protocol, sys.stdin.buffer,
        )

        while True:
            try:
                line = await reader.readline()
                if not line:
                    break

                request = json.loads(line.decode("utf-8").strip())
                response = await self._handle_jsonrpc(request)

                if response is not None:
                    output = json.dumps(response, default=str, ensure_ascii=False)
                    sys.stdout.write(output + "\n")
                    sys.stdout.flush()

            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error("ide_server.stdio_error", error=str(e))
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": str(e)},
                    "id": None,
                }
                sys.stdout.write(json.dumps(error_response) + "\n")
                sys.stdout.flush()

    async def _handle_jsonrpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Handle a single JSON-RPC request."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        result: Any = None

        if method == "initialize":
            result = self.get_server_info()
        elif method == "tools/list":
            result = {"tools": self.list_mcp_tools()}
        elif method == "tools/call":
            tool_name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = await self.handle_tool_call(tool_name, arguments)
            result = {"content": [{"type": "text", "text": json.dumps(result, default=str)}]}
        elif method == "resources/list":
            result = {"resources": self.list_mcp_resources()}
        elif method == "resources/read":
            uri = params.get("uri", "")
            content = await self.handle_resource_read(uri)
            result = {
                "contents": [{
                    "uri": uri,
                    "mimeType": "application/json",
                    "text": json.dumps(content, default=str),
                }],
            }
        elif method == "notifications/initialized":
            return None  # Notification, no response
        elif method == "ping":
            result = {}
        else:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Method not found: {method}"},
                "id": req_id,
            }

        return {
            "jsonrpc": "2.0",
            "result": result,
            "id": req_id,
        }


def create_mcp_server(
    config_path: str | None = None,
    workspace_dir: str | None = None,
) -> MCPIDEServer:
    """Factory function to create an MCP IDE server."""
    return MCPIDEServer(
        config_path=config_path,
        workspace_dir=workspace_dir,
    )


# ── CLI entry point ──────────────────────────────────────────────

def main() -> None:
    """Run the MCP server from command line."""
    import argparse

    parser = argparse.ArgumentParser(description="Agent Framework MCP IDE Server")
    parser.add_argument("--config", type=str, help="Config file path")
    parser.add_argument("--workspace", type=str, help="Workspace directory")
    args = parser.parse_args()

    server = create_mcp_server(
        config_path=args.config,
        workspace_dir=args.workspace,
    )
    asyncio.run(server.run_stdio())


if __name__ == "__main__":
    main()

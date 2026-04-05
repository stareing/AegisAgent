from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class MCPTransportType(str, Enum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable_http"


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server connection."""

    server_id: str
    transport: MCPTransportType = MCPTransportType.STDIO
    command: str | None = None          # for stdio
    args: list[str] = Field(default_factory=list)  # for stdio
    env: dict[str, str] = Field(default_factory=dict)  # for stdio
    url: str | None = None              # for sse / streamable_http
    headers: dict[str, str] = Field(default_factory=dict)  # for http
    auto_connect: bool = True
    tool_namespace: str | None = None   # override namespace prefix


class MCPToolInfo(BaseModel):
    """Tool metadata discovered from an MCP server."""

    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)
    server_id: str = ""


class MCPResourceInfo(BaseModel):
    """Resource metadata discovered from an MCP server."""

    name: str
    uri: str
    description: str = ""
    mime_type: str | None = None
    server_id: str = ""


class MCPResourceTemplateInfo(BaseModel):
    """Resource template metadata discovered from an MCP server."""

    name: str
    uri_template: str
    description: str = ""
    mime_type: str | None = None
    server_id: str = ""


class MCPPromptArgument(BaseModel):
    """A single argument for an MCP prompt."""

    name: str
    description: str = ""
    required: bool = False


class MCPPromptInfo(BaseModel):
    """Prompt metadata discovered from an MCP server."""

    name: str
    description: str = ""
    arguments: list[MCPPromptArgument] = Field(default_factory=list)
    server_id: str = ""

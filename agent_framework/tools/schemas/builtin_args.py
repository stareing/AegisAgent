"""Canonical parameter models for all built-in system tools.

Single source of truth for tool parameter contracts. Each model corresponds
to one built-in tool and defines the exact parameter interface.

These models are NOT directly used by the @tool decorator (which generates
schemas from function signatures). They serve as:
- Canonical reference for API consumers
- External validation layer for programmatic tool invocation
- Documentation generation source
- Contract testing targets
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Tool category constants — single definition, used by tools and policies
# ---------------------------------------------------------------------------

class ToolCategory:
    """Canonical tool categories for built-in system tools."""

    FILESYSTEM = "filesystem"
    SEARCH = "search"
    SYSTEM = "system"
    NETWORK = "network"
    DELEGATION = "delegation"
    CONTROL = "control"
    MEMORY_ADMIN = "memory_admin"
    REASONING = "reasoning"

    # Categories safe for sub-agents (read-only)
    SUBAGENT_SAFE: frozenset[str] = frozenset({
        FILESYSTEM, SEARCH, REASONING,
    })

    # Categories blocked for sub-agents by default
    SUBAGENT_BLOCKED: frozenset[str] = frozenset({
        SYSTEM, NETWORK, CONTROL, DELEGATION, MEMORY_ADMIN,
    })

    # High-risk categories requiring extra caution
    HIGH_RISK: frozenset[str] = frozenset({
        SYSTEM, NETWORK, DELEGATION,
    })


# Namespace for all built-in tools
SYSTEM_NAMESPACE = "system"


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------

class ReadFileArgs(BaseModel):
    """Parameters for read_file tool."""
    path: str = Field(description="The file path to read")
    encoding: str = Field(default="utf-8", description="File encoding")


class WriteFileArgs(BaseModel):
    """Parameters for write_file tool."""
    path: str = Field(description="The file path to write to")
    content: str = Field(description="The content to write")
    encoding: str = Field(default="utf-8", description="File encoding")


class EditFileArgs(BaseModel):
    """Parameters for edit_file tool."""
    file_path: str = Field(description="Absolute path to the file to modify")
    old_string: str = Field(description="The exact text to find and replace")
    new_string: str = Field(description="The replacement text")
    replace_all: bool = Field(
        default=False,
        description="Replace all occurrences (default: old_string must be unique)",
    )


class ListDirectoryArgs(BaseModel):
    """Parameters for list_directory tool."""
    path: str = Field(default=".", description="Directory path to list")
    pattern: str = Field(default="*", description="Glob pattern to filter results")


class FileExistsArgs(BaseModel):
    """Parameters for file_exists tool."""
    path: str = Field(description="The path to check")


# ---------------------------------------------------------------------------
# Search tools
# ---------------------------------------------------------------------------

class GrepSearchArgs(BaseModel):
    """Parameters for grep_search tool."""
    pattern: str = Field(description="Regular expression pattern to search for")
    path: str = Field(default=".", description="File or directory to search in")
    glob: str | None = Field(default=None, description="Glob pattern to filter files")
    case_insensitive: bool = Field(default=False, description="Ignore case when matching")
    context_lines: int = Field(default=0, description="Context lines before and after match")
    max_results: int = Field(default=50, description="Maximum number of matches")
    include_gitignored: bool = Field(
        default=False,
        description="Also search files ignored by .gitignore",
    )


class GlobFilesArgs(BaseModel):
    """Parameters for glob_files tool."""
    pattern: str = Field(description="Glob pattern (e.g. '**/*.py')")
    path: str = Field(default=".", description="Root directory to search from")
    max_results: int = Field(default=100, description="Maximum number of results")
    include_gitignored: bool = Field(
        default=False,
        description="Include files ignored by .gitignore",
    )


# ---------------------------------------------------------------------------
# Shell tools
# ---------------------------------------------------------------------------

class BashExecArgs(BaseModel):
    """Parameters for bash_exec tool."""
    command: str = Field(description="The shell command to execute")
    timeout_seconds: int = Field(
        default=120,
        description="Maximum execution time (max 600s)",
    )
    run_in_background: bool = Field(
        default=False,
        description="Run in background and return a task_id",
    )
    description: str = Field(
        default="",
        description="Brief description of what the command does",
    )


class BashOutputArgs(BaseModel):
    """Parameters for bash_output tool."""
    task_id: str = Field(description="Task ID from bash_exec background execution")


# ---------------------------------------------------------------------------
# System tools
# ---------------------------------------------------------------------------

class RunCommandArgs(BaseModel):
    """Parameters for run_command tool."""
    command: str = Field(description="The shell command to execute")
    timeout_seconds: int = Field(default=30, description="Maximum execution time")
    cwd: str | None = Field(default=None, description="Working directory")


class GetEnvArgs(BaseModel):
    """Parameters for get_env tool."""
    name: str = Field(description="The environment variable name")
    default: str = Field(default="", description="Default value if not set")


# ---------------------------------------------------------------------------
# Web tools
# ---------------------------------------------------------------------------

class WebFetchArgs(BaseModel):
    """Parameters for web_fetch tool."""
    url: str = Field(description="The URL to fetch")
    timeout_seconds: int = Field(default=30, description="Request timeout in seconds")
    extract_text: bool = Field(
        default=True,
        description="Extract readable text from HTML (vs raw content)",
    )


class WebSearchArgs(BaseModel):
    """Parameters for web_search tool."""
    query: str = Field(description="Search query string")
    max_results: int = Field(default=5, description="Maximum number of results")
    allowed_domains: list[str] | None = Field(
        default=None,
        description="Only return results from these domains",
    )
    blocked_domains: list[str] | None = Field(
        default=None,
        description="Exclude results from these domains",
    )


# ---------------------------------------------------------------------------
# Notebook tools
# ---------------------------------------------------------------------------

class NotebookEditArgs(BaseModel):
    """Parameters for notebook_edit tool."""
    file_path: str = Field(description="Path to the .ipynb file")
    cell_index: int = Field(description="Zero-based index of the cell")
    new_source: str | None = Field(
        default=None,
        description="New source content for the cell",
    )
    cell_type: str | None = Field(
        default=None,
        description="Cell type: 'code', 'markdown', 'raw'",
    )
    action: Literal["replace", "insert_before", "insert_after", "delete"] = Field(
        default="replace",
        description="Edit action to perform",
    )


# ---------------------------------------------------------------------------
# Task management tools
# ---------------------------------------------------------------------------

class TodoWriteArgs(BaseModel):
    """Parameters for todo_write tool."""
    tasks: str = Field(
        description="JSON array of task objects with title, status, priority",
    )


# ---------------------------------------------------------------------------
# Control tools
# ---------------------------------------------------------------------------

class SlashCommandArgs(BaseModel):
    """Parameters for slash_command tool."""
    command: str = Field(description="The slash command to execute (e.g. '/help')")


# ---------------------------------------------------------------------------
# Delegation tools
# ---------------------------------------------------------------------------

class SpawnAgentArgs(BaseModel):
    """Parameters for spawn_agent tool."""
    task_input: str = Field(description="Task description for the sub-agent")
    mode: str = Field(default="EPHEMERAL", description="EPHEMERAL, FORK, or LONG_LIVED")
    skill_id: str | None = Field(default=None, description="Skill to activate")
    tool_categories: list[str] | None = Field(
        default=None,
        description="Tool categories the sub-agent can use",
    )
    memory_scope: str = Field(
        default="ISOLATED",
        description="ISOLATED, INHERIT_READ, or SHARED_WRITE",
    )
    token_budget: int = Field(default=4096, description="Max token budget for context")
    max_iterations: int = Field(default=10, description="Max iterations")
    deadline_ms: int = Field(default=0, description="Execution deadline in ms (0=no limit)")
    wait: bool = Field(
        default=True,
        description="Block until completion (True) or return spawn_id (False)",
    )


class CheckSpawnResultArgs(BaseModel):
    """Parameters for check_spawn_result tool."""
    spawn_id: str = Field(description="Spawn ID from spawn_agent(wait=false)")
    wait: bool = Field(
        default=True,
        description="Block until completion (True) or check status (False)",
    )


# ---------------------------------------------------------------------------
# Memory admin tools
# ---------------------------------------------------------------------------

class ListMemoriesArgs(BaseModel):
    """Parameters for list_memories tool."""
    user_id: str | None = Field(default=None, description="User ID for namespace isolation")


class ForgetMemoryArgs(BaseModel):
    """Parameters for forget_memory tool."""
    memory_id: str = Field(description="The memory ID to delete")


class ClearMemoriesArgs(BaseModel):
    """Parameters for clear_memories tool."""
    user_id: str | None = Field(default=None, description="User ID for namespace isolation")


# ---------------------------------------------------------------------------
# Reasoning tools
# ---------------------------------------------------------------------------

class ThinkArgs(BaseModel):
    """Parameters for think tool."""
    thought: str = Field(description="Reasoning, analysis, or plan content")

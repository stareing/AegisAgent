"""Centralized parameter models for built-in system tools.

All builtin tool parameter schemas live here as single source of truth.
The @tool decorator still auto-generates schemas from function signatures,
but these models serve as canonical reference and can be used for
external validation, documentation generation, and API contract testing.
"""

from agent_framework.tools.schemas.builtin_args import (
    # Filesystem
    ReadFileArgs,
    WriteFileArgs,
    EditFileArgs,
    ListDirectoryArgs,
    FileExistsArgs,
    # Search
    GrepSearchArgs,
    GlobFilesArgs,
    # Shell
    BashExecArgs,
    BashOutputArgs,
    # System
    RunCommandArgs,
    GetEnvArgs,
    # Web
    WebFetchArgs,
    WebSearchArgs,
    # Notebook
    NotebookEditArgs,
    # Task graph
    TaskCreateArgs,
    TaskUpdateArgs,
    TaskGetArgs,
    # Control
    SlashCommandArgs,
    # Delegation
    SpawnAgentArgs,
    CheckSpawnResultArgs,
    # Memory
    ListMemoriesArgs,
    ForgetMemoryArgs,
    ClearMemoriesArgs,
    # Reasoning
    ThinkArgs,
)

__all__ = [
    "ReadFileArgs", "WriteFileArgs", "EditFileArgs", "ListDirectoryArgs",
    "FileExistsArgs", "GrepSearchArgs", "GlobFilesArgs",
    "BashExecArgs", "BashOutputArgs", "RunCommandArgs", "GetEnvArgs",
    "WebFetchArgs", "WebSearchArgs", "NotebookEditArgs",
    "TaskCreateArgs", "TaskUpdateArgs", "TaskGetArgs",
    "SlashCommandArgs", "SpawnAgentArgs", "CheckSpawnResultArgs",
    "ListMemoriesArgs", "ForgetMemoryArgs", "ClearMemoriesArgs", "ThinkArgs",
]

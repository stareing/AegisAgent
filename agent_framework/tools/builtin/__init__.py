"""Built-in system tools that ship with the framework.

Call ``register_all_builtins(catalog)`` to register every built-in tool
into a GlobalToolCatalog in one shot.

Tool categories (aligned with system tools spec):
- filesystem: read_file, write_file, edit_file, list_directory, file_exists,
              glob_files, grep_search, notebook_edit
- system: bash_exec, bash_output, bash_stop, task_stop, kill_shell, run_command, get_env
- network: web_fetch, web_search
- delegation: spawn_agent, check_spawn_result, send_message, close_agent
- control: task_create, task_update, task_list, task_get, slash_command, exit_plan_mode
- memory_admin: list_memories, forget_memory, clear_memories
- reasoning: think

Sub-agent default policy:
- ALLOWED: filesystem (read-only: read_file, list_directory, file_exists,
           glob_files, grep_search), reasoning
- BLOCKED: system, network, control, delegation, memory_admin
- BLOCKED: filesystem write tools (write_file, edit_file, notebook_edit)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.tools.schemas.builtin_args import ToolCategory

if TYPE_CHECKING:
    from agent_framework.models.agent import CapabilityPolicy
    from agent_framework.tools.catalog import GlobalToolCatalog


# Sub-agent default blocked categories
SUBAGENT_DEFAULT_BLOCKED_CATEGORIES: frozenset[str] = ToolCategory.SUBAGENT_BLOCKED

# Write tools that sub-agents should not have access to by default
SUBAGENT_BLOCKED_WRITE_TOOLS: frozenset[str] = frozenset({
    "write_file", "edit_file", "notebook_edit",
})


def build_subagent_default_policy() -> CapabilityPolicy:
    """Build the default CapabilityPolicy for sub-agents.

    Blocks: system, network, control, delegation, memory_admin categories.
    Allows: filesystem (read-only), reasoning.
    """
    from agent_framework.models.agent import CapabilityPolicy
    return CapabilityPolicy(
        blocked_tool_categories=list(SUBAGENT_DEFAULT_BLOCKED_CATEGORIES),
        allow_network_tools=False,
        allow_system_tools=False,
        allow_spawn=False,
        max_spawn_depth=0,
        allow_memory_admin=False,
    )


def register_all_builtins(
    catalog: GlobalToolCatalog,
    *,
    shell_enabled: bool = False,
    web_search_enabled: bool = True,
    control_tools_enabled: bool = True,
) -> int:
    """Register all built-in tools into the given catalog.

    Args:
        catalog: The tool catalog to register into.
        shell_enabled: When False (default), shell and system tools
            (bash_exec, bash_output, bash_stop, task_stop, kill_shell, run_command) are
            not registered. get_env is always registered but requires
            confirmation.
        web_search_enabled: Register web_search tool (default True).
        control_tools_enabled: Register control tools (slash_command,
            exit_plan_mode) (default True).

    Returns the number of tools registered.
    """
    from agent_framework.tools.builtin.filesystem import (
        read_file,
        write_file,
        list_directory,
        # file_exists,  # redundant: bash "test -f" or list_directory
    )
    # from agent_framework.tools.builtin.system import run_command, get_env
    #   run_command — redundant with bash_exec
    #   get_env — bash "echo $VAR" covers this
    from agent_framework.tools.builtin.spawn_agent import spawn_agent, check_spawn_result, send_message, close_agent
    from agent_framework.tools.builtin.team_tools import team, mail
    from agent_framework.tools.builtin.code_edit import edit_file  # notebook_edit: niche, use write_file
    from agent_framework.tools.builtin.search import grep_search, glob_files
    from agent_framework.tools.builtin.shell import (
        bash_exec, bash_output, bash_stop, task_stop, kill_shell,
    )
    from agent_framework.tools.builtin.web import web_fetch, web_search
    from agent_framework.tools.builtin.task_manager import (
        task_create, task_update, task_list, task_get,
    )
    from agent_framework.tools.builtin.think import think
    from agent_framework.tools.builtin.memory_admin import (
        list_memories, forget_memory, clear_memories,
    )
    from agent_framework.tools.builtin_skills import invoke_skill

    builtins = [
        # Filesystem (read)
        read_file, list_directory,
        # Filesystem (write)
        write_file, edit_file,
        # Filesystem (search)
        grep_search, glob_files,
        # Network
        web_fetch,
        # Control (task graph)
        task_create, task_update, task_list, task_get,
        # Reasoning
        think,
        # Delegation
        spawn_agent, check_spawn_result, send_message, close_agent,
        # Team
        team, mail,
        # Skills
        invoke_skill,
        # Memory admin (not exposed to Agent by default — requires capability policy)
        list_memories, forget_memory, clear_memories,
    ]

    # Web search (optional, enabled by default)
    if web_search_enabled:
        builtins.append(web_search)

    # Shell/system tools — bash_exec is the primary shell interface
    if shell_enabled:
        builtins.extend([bash_exec, bash_output, bash_stop, task_stop, kill_shell])

    # Control tools (optional, enabled by default)
    if control_tools_enabled:
        from agent_framework.tools.builtin.control_tools import (
            # slash_command,  # integration-layer specific, not for agent
            exit_plan_mode,
        )
        builtins.extend([exit_plan_mode])

    count = 0
    for func in builtins:
        catalog.register_function(func)
        count += 1
    return count

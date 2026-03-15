"""Built-in tools that ship with the framework.

Call ``register_all_builtins(catalog)`` to register every built-in tool
into a GlobalToolCatalog in one shot.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_framework.tools.catalog import GlobalToolCatalog


def register_all_builtins(catalog: GlobalToolCatalog) -> int:
    """Register all built-in tools into the given catalog.

    Returns the number of tools registered.
    """
    from agent_framework.tools.builtin.filesystem import (
        read_file,
        write_file,
        list_directory,
        file_exists,
    )
    from agent_framework.tools.builtin.system import run_command, get_env
    from agent_framework.tools.builtin.spawn_agent import spawn_agent, check_spawn_result
    from agent_framework.tools.builtin.code_edit import edit_file, notebook_edit
    from agent_framework.tools.builtin.search import grep_search, glob_files
    from agent_framework.tools.builtin.shell import bash_exec, bash_output, kill_shell
    from agent_framework.tools.builtin.web import web_fetch
    from agent_framework.tools.builtin.task_manager import todo_write, todo_read
    from agent_framework.tools.builtin.think import think
    from agent_framework.tools.builtin_skills import invoke_skill

    builtins = [
        # Filesystem
        read_file, write_file, list_directory, file_exists,
        # Code editing
        edit_file, notebook_edit,
        # Search
        grep_search, glob_files,
        # System / Shell
        run_command, get_env,
        bash_exec, bash_output, kill_shell,
        # Web
        web_fetch,
        # Task management
        todo_write, todo_read,
        # Reasoning
        think,
        # Sub-agents
        spawn_agent, check_spawn_result,
        # Skills
        invoke_skill,
    ]
    count = 0
    for func in builtins:
        catalog.register_function(func)
        count += 1
    return count

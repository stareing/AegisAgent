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
    from agent_framework.tools.builtin.spawn_agent import spawn_agent
    from agent_framework.tools.builtin_skills import invoke_skill

    builtins = [read_file, write_file, list_directory, file_exists, run_command, get_env, spawn_agent, invoke_skill]
    count = 0
    for func in builtins:
        catalog.register_function(func)
        count += 1
    return count
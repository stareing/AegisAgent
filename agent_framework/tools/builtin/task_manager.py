"""Built-in task graph tools — schema layer.

Four tools for the persistent task DAG:
- ``task_create``: Create a task with optional dependencies
- ``task_update``: Update status / add dependencies / assign owner / merge metadata
- ``task_list``:   List all tasks with ready/blocked/completed summary
- ``task_get``:    Get a single task by ID

Actual execution is routed through ToolExecutor → TaskService → TaskManager.
These functions serve as schema source and standalone fallback.
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE


@tool(
    name="task_create",
    description=(
        "Create a new task in the persistent task graph. "
        "USE PROACTIVELY when: the task requires 3+ steps, "
        "the user asks for a plan/todo, or multiple tasks are given. "
        "Mark in_progress before starting, completed when done. "
        "Supports dependencies via blocked_by to define execution order."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
    search_hint="create task todo item",
)
def task_create(
    subject: str,
    description: str = "",
    blocked_by: list[int] | None = None,
    active_form: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Create a new task.

    Args:
        subject: Short task title (imperative form, e.g. "Fix auth bug").
        description: Detailed description with context and acceptance criteria.
        blocked_by: Task IDs that must complete before this task can start.
        active_form: Present continuous form shown when in_progress
            (e.g. "Fixing auth bug"). Falls back to subject if empty.
        metadata: Arbitrary key-value pairs to attach to the task.

    Returns:
        JSON string of the created task.
    """
    from agent_framework.tools.todo import TaskManager
    mgr = TaskManager()
    return mgr.create(subject, description, blocked_by, active_form, metadata)


@tool(
    name="task_update",
    description=(
        "Update a task's status or details. "
        "MUST set to 'in_progress' before starting work on a task, "
        "and 'completed' immediately after finishing. "
        "Setting 'completed' auto-unblocks dependent tasks. "
        "Setting 'deleted' removes the task permanently."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
    search_hint="update task status progress",
)
def task_update(
    task_id: int,
    status: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    active_form: str | None = None,
    add_blocked_by: list[int] | None = None,
    add_blocks: list[int] | None = None,
    owner: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Update a task.

    Args:
        task_id: ID of the task to update.
        status: New status ('pending', 'in_progress', 'completed', 'deleted').
        subject: New subject text.
        description: New description.
        active_form: Present continuous form for spinner display.
        add_blocked_by: Task IDs to add as upstream dependencies.
        add_blocks: Task IDs to add as downstream dependents.
        owner: Agent/user who owns this task.
        metadata: Keys to merge into metadata. Set a key to null to delete it.

    Returns:
        JSON string of the updated task, or {"id":N,"status":"deleted"}.
    """
    from agent_framework.tools.todo import TaskManager
    mgr = TaskManager()
    return mgr.update(
        task_id, status=status, subject=subject, description=description,
        active_form=active_form, add_blocked_by=add_blocked_by,
        add_blocks=add_blocks, owner=owner, metadata=metadata,
    )


@tool(
    name="task_list",
    description=(
        "List all tasks with status summary. "
        "Call after completing a task to find the next one to work on."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
    is_read_only=True,
    search_hint="list tasks todo items",
)
def task_list() -> str:
    """List all tasks with summary.

    Returns:
        JSON with tasks array, summary counts, and ready/blocked task IDs.
    """
    from agent_framework.tools.todo import TaskManager
    mgr = TaskManager()
    return mgr.list_all()


@tool(
    name="task_get",
    description="Get details of a single task by its ID.",
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
    is_read_only=True,
    search_hint="get task details",
)
def task_get(task_id: int) -> str:
    """Get a single task.

    Args:
        task_id: The task ID to retrieve.

    Returns:
        JSON string of the task, or error if not found.
    """
    from agent_framework.tools.todo import TaskManager
    mgr = TaskManager()
    return mgr.get(task_id)

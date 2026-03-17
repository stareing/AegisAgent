"""Built-in task graph tools — schema layer.

Four tools for the persistent task DAG:
- ``task_create``: Create a task with optional dependencies
- ``task_update``: Update status / add dependencies / assign owner
- ``task_list``:   List all tasks with ready/blocked/completed summary
- ``task_get``:    Get a single task by ID

Actual execution is routed through ToolExecutor → TaskService → TaskManager.
These functions serve as schema source and standalone fallback.
"""

from __future__ import annotations

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE


@tool(
    name="task_create",
    description=(
        "Create a new task in the persistent task graph. "
        "Use for multi-step work that benefits from structured tracking. "
        "Do NOT use for simple questions or single-action requests. "
        "Supports dependencies via blocked_by to define execution order."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
)
def task_create(
    subject: str,
    description: str = "",
    blocked_by: list[int] | None = None,
) -> str:
    """Create a new task.

    Args:
        subject: Short task title.
        description: Detailed task description.
        blocked_by: List of task IDs that must complete before this task can start.

    Returns:
        JSON string of the created task.
    """
    from agent_framework.tools.todo import TaskManager
    mgr = TaskManager()
    return mgr.create(subject, description, blocked_by)


@tool(
    name="task_update",
    description=(
        "Update an existing task: change status, add dependencies, or assign owner. "
        "Setting status to 'completed' auto-unblocks downstream dependent tasks."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
)
def task_update(
    task_id: int,
    status: str | None = None,
    subject: str | None = None,
    description: str | None = None,
    add_blocked_by: list[int] | None = None,
    add_blocks: list[int] | None = None,
    owner: str | None = None,
) -> str:
    """Update a task.

    Args:
        task_id: ID of the task to update.
        status: New status ('pending', 'in_progress', 'completed').
        subject: New subject text.
        description: New description.
        add_blocked_by: Task IDs to add as upstream dependencies.
        add_blocks: Task IDs to add as downstream dependents.
        owner: Agent/user who owns this task.

    Returns:
        JSON string of the updated task.
    """
    from agent_framework.tools.todo import TaskManager
    mgr = TaskManager()
    return mgr.update(
        task_id, status=status, subject=subject, description=description,
        add_blocked_by=add_blocked_by, add_blocks=add_blocks, owner=owner,
    )


@tool(
    name="task_list",
    description=(
        "List all tasks with dependency graph and status summary. "
        "Shows ready (can start now), blocked (waiting), in-progress, and completed tasks."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
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

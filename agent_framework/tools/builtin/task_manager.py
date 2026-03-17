"""Built-in task management tools.

Provides structured task list creation and tracking with status
management (pending / in_progress / completed).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE


class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


@dataclass
class TaskItem:
    id: str
    title: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "priority": self.priority,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class _TaskStore:
    """In-memory task store, singleton per process."""

    _instance: _TaskStore | None = None

    def __init__(self) -> None:
        self._tasks: dict[str, TaskItem] = {}
        self._counter = 0

    @classmethod
    def get(cls) -> _TaskStore:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def add(self, title: str, priority: int = 0) -> TaskItem:
        self._counter += 1
        task_id = f"task-{self._counter}"
        task = TaskItem(id=task_id, title=title, priority=priority)
        self._tasks[task_id] = task
        return task

    def update(self, task_id: str, status: TaskStatus | None = None, title: str | None = None) -> TaskItem:
        task = self._tasks.get(task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")
        if status is not None:
            task.status = status
        if title is not None:
            task.title = title
        task.updated_at = time.time()
        return task

    def remove(self, task_id: str) -> bool:
        return self._tasks.pop(task_id, None) is not None

    def list_all(self) -> list[TaskItem]:
        return sorted(
            self._tasks.values(),
            key=lambda t: (-t.priority, t.created_at),
        )

    def clear(self) -> int:
        count = len(self._tasks)
        self._tasks.clear()
        return count

    def write_bulk(self, tasks: list[dict]) -> list[TaskItem]:
        """Bulk create/update tasks from a list of dicts.

        Each dict should have 'title' (required), and optionally
        'id', 'status', 'priority'.
        """
        results: list[TaskItem] = []
        for spec in tasks:
            title = spec.get("title", "")
            if not title:
                continue
            task_id = spec.get("id")
            if task_id and task_id in self._tasks:
                # Update existing
                task = self.update(
                    task_id,
                    status=TaskStatus(spec["status"]) if "status" in spec else None,
                    title=title,
                )
            else:
                # Create new
                task = self.add(title, priority=spec.get("priority", 0))
                if "status" in spec:
                    task.status = TaskStatus(spec["status"])
            results.append(task)
        return results


@tool(
    name="todo_write",
    description=(
        "Create and manage a structured task list. "
        "Accepts a list of tasks with title, status (pending/in_progress/completed), "
        "and priority. Updates existing tasks by id, creates new ones otherwise."
    ),
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
)
def todo_write(tasks: str) -> dict:
    """Create or update tasks in the task list.

    Args:
        tasks: JSON string of task list. Each task is an object with:
               - title (required): Task description
               - id (optional): Task ID to update existing task
               - status (optional): 'pending', 'in_progress', or 'completed'
               - priority (optional): Higher number = higher priority

    Returns:
        Dict with created/updated tasks and summary.
    """
    try:
        task_list = json.loads(tasks)
    except json.JSONDecodeError:
        raise ValueError("tasks must be a valid JSON array of task objects")

    if not isinstance(task_list, list):
        task_list = [task_list]

    store = _TaskStore.get()
    results = store.write_bulk(task_list)

    return {
        "tasks": [t.to_dict() for t in results],
        "total_tasks": len(store.list_all()),
    }


@tool(
    name="todo_read",
    description="Read the current task list with status and progress summary.",
    category="control",
    require_confirm=False,
    tags=["system", "control", "task"],
    namespace=SYSTEM_NAMESPACE,
)
def todo_read() -> dict:
    """Read all tasks from the task list.

    Returns:
        Dict with 'tasks' list and progress summary.
    """
    store = _TaskStore.get()
    all_tasks = store.list_all()

    pending = sum(1 for t in all_tasks if t.status == TaskStatus.PENDING)
    in_progress = sum(1 for t in all_tasks if t.status == TaskStatus.IN_PROGRESS)
    completed = sum(1 for t in all_tasks if t.status == TaskStatus.COMPLETED)

    return {
        "tasks": [t.to_dict() for t in all_tasks],
        "summary": {
            "total": len(all_tasks),
            "pending": pending,
            "in_progress": in_progress,
            "completed": completed,
        },
    }

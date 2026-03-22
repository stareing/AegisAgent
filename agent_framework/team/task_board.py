"""TeamTaskBoard — shared task panel with claim, dependency, and file-lock semantics.

Aligns with Claude Code Agent Teams' shared task list:
- Tasks have states: pending → in_progress → completed (or failed/blocked)
- Tasks can depend on other tasks (auto-unblock on completion)
- Teammates self-claim via atomic lock
- Lead can also assign explicitly

Thread-safe via threading.Lock (equivalent to Claude Code's file-lock approach).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class TaskStatus(str, Enum):
    """Task lifecycle states."""

    PENDING = "pending"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_TASK_STATUSES: frozenset[TaskStatus] = frozenset({
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
})


class TeamTask(BaseModel):
    """A single task on the shared board."""

    task_id: str = Field(default_factory=lambda: f"task_{uuid.uuid4().hex[:8]}")
    team_id: str = ""
    title: str
    description: str = ""
    status: TaskStatus = TaskStatus.PENDING
    assigned_to: str = ""
    created_by: str = ""
    depends_on: list[str] = Field(default_factory=list)
    result: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TeamTaskBoard:
    """Thread-safe shared task board for agent teams.

    Responsibilities:
    - CRUD for tasks
    - Dependency tracking with auto-unblock
    - Atomic claim (threading.Lock prevents race conditions)
    - Queries: list all, list claimable, list by assignee
    """

    def __init__(self, team_id: str) -> None:
        self._team_id = team_id
        self._tasks: dict[str, TeamTask] = {}
        self._lock = threading.Lock()

    @property
    def team_id(self) -> str:
        return self._team_id

    def create_task(
        self,
        title: str,
        description: str = "",
        depends_on: list[str] | None = None,
        created_by: str = "",
    ) -> TeamTask:
        """Create a new task. Auto-sets BLOCKED if dependencies are unresolved."""
        with self._lock:
            task = TeamTask(
                team_id=self._team_id,
                title=title,
                description=description,
                depends_on=depends_on or [],
                created_by=created_by,
            )
            if task.depends_on and self._is_blocked_unlocked(task):
                task.status = TaskStatus.BLOCKED

            self._tasks[task.task_id] = task
            logger.info("team.task_created", task_id=task.task_id,
                        title=title[:80], status=task.status.value)
            return task

    def claim_task(self, agent_id: str, task_id: str = "") -> TeamTask | None:
        """Atomically claim a task.

        If task_id is provided, claims that specific task (must be PENDING + unblocked).
        If empty, claims the next available task in creation order.
        Returns the claimed task, or None if nothing available.
        """
        with self._lock:
            if task_id:
                task = self._tasks.get(task_id)
                if (task and task.status == TaskStatus.PENDING
                        and not self._is_blocked_unlocked(task)):
                    task.status = TaskStatus.IN_PROGRESS
                    task.assigned_to = agent_id
                    task.updated_at = datetime.now(timezone.utc)
                    logger.info("team.task_claimed", task_id=task_id,
                                agent_id=agent_id, title=task.title[:80])
                    return task
                return None

            # Auto-claim: first PENDING + unblocked
            for t in self._tasks.values():
                if (t.status == TaskStatus.PENDING
                        and not self._is_blocked_unlocked(t)):
                    t.status = TaskStatus.IN_PROGRESS
                    t.assigned_to = agent_id
                    t.updated_at = datetime.now(timezone.utc)
                    logger.info("team.task_claimed", task_id=t.task_id,
                                agent_id=agent_id, title=t.title[:80])
                    return t
            return None

    def complete_task(
        self, task_id: str, result: str = "", agent_id: str = "",
    ) -> TeamTask | None:
        """Mark task as completed. Auto-unblocks dependent tasks."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status not in (TaskStatus.IN_PROGRESS, TaskStatus.PENDING):
                return None
            task.status = TaskStatus.COMPLETED
            task.result = result
            task.updated_at = datetime.now(timezone.utc)
            self._unblock_dependents_unlocked(task_id)
            logger.info("team.task_completed", task_id=task_id,
                        title=task.title[:80])
            return task

    def fail_task(self, task_id: str, error: str = "") -> TeamTask | None:
        """Mark task as failed."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.status = TaskStatus.FAILED
            task.result = error
            task.updated_at = datetime.now(timezone.utc)
            logger.info("team.task_failed", task_id=task_id,
                        title=task.title[:80], error=error[:100])
            return task

    def list_tasks(
        self,
        status: TaskStatus | None = None,
        assigned_to: str = "",
    ) -> list[TeamTask]:
        """List tasks, optionally filtered by status or assignee."""
        with self._lock:
            result = list(self._tasks.values())
            if status is not None:
                result = [t for t in result if t.status == status]
            if assigned_to:
                result = [t for t in result if t.assigned_to == assigned_to]
            return result

    def list_claimable(self) -> list[TeamTask]:
        """List tasks that can be claimed (PENDING + no unresolved deps)."""
        with self._lock:
            return [
                t for t in self._tasks.values()
                if t.status == TaskStatus.PENDING
                and not self._is_blocked_unlocked(t)
            ]

    def get_task(self, task_id: str) -> TeamTask | None:
        """Look up a single task."""
        with self._lock:
            return self._tasks.get(task_id)

    def task_count(self) -> dict[str, int]:
        """Return count by status."""
        with self._lock:
            counts: dict[str, int] = {}
            for t in self._tasks.values():
                counts[t.status.value] = counts.get(t.status.value, 0) + 1
            return counts

    # --- Internal (caller holds lock) ---

    def _is_blocked_unlocked(self, task: TeamTask) -> bool:
        for dep_id in task.depends_on:
            dep = self._tasks.get(dep_id)
            if dep is None or dep.status != TaskStatus.COMPLETED:
                return True
        return False

    def _unblock_dependents_unlocked(self, completed_task_id: str) -> None:
        for t in self._tasks.values():
            if (t.status == TaskStatus.BLOCKED
                    and completed_task_id in t.depends_on
                    and not self._is_blocked_unlocked(t)):
                t.status = TaskStatus.PENDING
                t.updated_at = datetime.now(timezone.utc)
                logger.info("team.task_unblocked", task_id=t.task_id,
                            title=t.title[:80])

"""Run-scoped TaskManager — persistent task graph with dependencies.

Upgrades the flat TodoManager to a DAG-structured task system:
- Each task has ``blockedBy`` (upstream deps) and ``blocks`` (downstream deps)
- Completing a task auto-unblocks dependents
- Tasks are persisted to disk as JSON files under ``.tasks/``
- Survives context compression and process restart

Three questions the graph always answers:
1. What can I do? → ``pending`` + ``blockedBy`` is empty
2. What is blocked? → ``pending`` + ``blockedBy`` is non-empty
3. What is done? → ``completed``

Run-scoped via ``TaskService`` (maps run_id → TaskManager).
"""

from __future__ import annotations

import json
import os
import time
from enum import Enum
from pathlib import Path
from typing import Any


# ── Models ─────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


# ── TaskManager — persistent DAG ──────────────────────────────────

_REMINDER_THRESHOLD = 3


class TaskManager:
    """Persistent task graph backed by per-task JSON files.

    File layout::

        .tasks/
          task_1.json  {"id": 1, "subject": "...", "status": "completed", ...}
          task_2.json  {"id": 2, "blockedBy": [1], "status": "pending", ...}
    """

    def __init__(self, tasks_dir: str | Path = ".tasks") -> None:
        self.dir = Path(tasks_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._next_id = self._max_id() + 1
        self._rounds_since_write = 0

    # ── CRUD ───────────────────────────────────────────────────────

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
    ) -> str:
        """Create a new task. Returns JSON representation."""
        task_id = self._next_id
        self._next_id += 1

        task: dict[str, Any] = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "status": TaskStatus.PENDING.value,
            "blockedBy": blocked_by or [],
            "blocks": [],
            "owner": "",
            "created_at": time.time(),
            "updated_at": time.time(),
        }

        # Register forward edges: add this task to the ``blocks`` list
        # of each upstream dependency.
        for dep_id in task["blockedBy"]:
            dep = self._load(dep_id)
            if dep and task_id not in dep.get("blocks", []):
                dep.setdefault("blocks", []).append(task_id)
                self._save(dep)

        self._save(task)
        self._rounds_since_write = 0
        return json.dumps(task, indent=2)

    def update(
        self,
        task_id: int,
        *,
        status: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
        owner: str | None = None,
    ) -> str:
        """Update an existing task. Returns updated JSON."""
        task = self._load(task_id)
        if task is None:
            return json.dumps({"error": f"Task {task_id} not found"})

        if subject is not None:
            task["subject"] = subject
        if description is not None:
            task["description"] = description
        if owner is not None:
            task["owner"] = owner

        # Add new dependency edges
        if add_blocked_by:
            for dep_id in add_blocked_by:
                if dep_id not in task.get("blockedBy", []):
                    task.setdefault("blockedBy", []).append(dep_id)
                    # Register forward edge on the upstream task
                    dep = self._load(dep_id)
                    if dep and task_id not in dep.get("blocks", []):
                        dep.setdefault("blocks", []).append(task_id)
                        self._save(dep)

        if add_blocks:
            for downstream_id in add_blocks:
                if downstream_id not in task.get("blocks", []):
                    task.setdefault("blocks", []).append(downstream_id)
                    # Register backward edge on the downstream task
                    downstream = self._load(downstream_id)
                    if downstream and task_id not in downstream.get("blockedBy", []):
                        downstream.setdefault("blockedBy", []).append(task_id)
                        self._save(downstream)

        # Status transition
        if status is not None:
            old_status = task.get("status")
            task["status"] = status
            if status == TaskStatus.COMPLETED.value and old_status != TaskStatus.COMPLETED.value:
                self._clear_dependency(task_id)

        task["updated_at"] = time.time()
        self._save(task)
        self._rounds_since_write = 0
        return json.dumps(task, indent=2)

    def get(self, task_id: int) -> str:
        """Get a single task by ID. Returns JSON."""
        task = self._load(task_id)
        if task is None:
            return json.dumps({"error": f"Task {task_id} not found"})
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        """List all tasks with summary. Returns JSON."""
        tasks = self._load_all()
        tasks.sort(key=lambda t: t.get("id", 0))

        ready = [t for t in tasks if t["status"] == "pending" and not t.get("blockedBy")]
        blocked = [t for t in tasks if t["status"] == "pending" and t.get("blockedBy")]
        in_progress = [t for t in tasks if t["status"] == "in_progress"]
        completed = [t for t in tasks if t["status"] == "completed"]

        return json.dumps({
            "tasks": tasks,
            "summary": {
                "total": len(tasks),
                "ready": len(ready),
                "blocked": len(blocked),
                "in_progress": len(in_progress),
                "completed": len(completed),
            },
            "ready_task_ids": [t["id"] for t in ready],
            "blocked_task_ids": [t["id"] for t in blocked],
        }, indent=2)

    # ── Dependency resolution ──────────────────────────────────────

    def _clear_dependency(self, completed_id: int) -> None:
        """Remove ``completed_id`` from every task's ``blockedBy``."""
        for path in self.dir.glob("task_*.json"):
            try:
                task = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            blocked_by = task.get("blockedBy", [])
            if completed_id in blocked_by:
                blocked_by.remove(completed_id)
                task["blockedBy"] = blocked_by
                task["updated_at"] = time.time()
                self._save(task)

    # ── Round tracking (for coordinator reminder) ──────────────────

    def mark_round(self, wrote_task: bool) -> None:
        """Called after each iteration by the coordinator."""
        if wrote_task:
            self._rounds_since_write = 0
        else:
            self._rounds_since_write += 1

    def should_remind(self) -> bool:
        """True when >=3 rounds passed without task tool call AND tasks exist."""
        return self.has_tasks and self._rounds_since_write >= _REMINDER_THRESHOLD

    @property
    def rounds_since_write(self) -> int:
        return self._rounds_since_write

    @property
    def has_tasks(self) -> bool:
        return any(self.dir.glob("task_*.json"))

    # ── Summary for context injection ──────────────────────────────

    def summary_text(self) -> str:
        """One-line summary for runtime_info injection."""
        if not self.has_tasks:
            return ""
        tasks = self._load_all()
        total = len(tasks)
        completed = sum(1 for t in tasks if t["status"] == "completed")
        in_prog = sum(1 for t in tasks if t["status"] == "in_progress")
        ready = sum(
            1 for t in tasks
            if t["status"] == "pending" and not t.get("blockedBy")
        )
        blocked = sum(
            1 for t in tasks
            if t["status"] == "pending" and t.get("blockedBy")
        )
        parts = [f"{completed}/{total} done"]
        if in_prog:
            parts.append(f"{in_prog} active")
        if ready:
            parts.append(f"{ready} ready")
        if blocked:
            parts.append(f"{blocked} blocked")
        return ", ".join(parts)

    # ── File I/O ───────────────────────────────────────────────────

    def _path(self, task_id: int) -> Path:
        return self.dir / f"task_{task_id}.json"

    def _save(self, task: dict) -> None:
        self._path(task["id"]).write_text(
            json.dumps(task, indent=2, ensure_ascii=False)
        )

    def _load(self, task_id: int) -> dict | None:
        path = self._path(task_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _load_all(self) -> list[dict]:
        tasks: list[dict] = []
        for path in sorted(self.dir.glob("task_*.json")):
            try:
                tasks.append(json.loads(path.read_text()))
            except (json.JSONDecodeError, OSError):
                continue
        return tasks

    def _max_id(self) -> int:
        max_id = 0
        for path in self.dir.glob("task_*.json"):
            try:
                data = json.loads(path.read_text())
                max_id = max(max_id, int(data.get("id", 0)))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
        return max_id


# ── TaskService — run-scoped registry ──────────────────────────────

class TaskService:
    """Maps run_id → TaskManager. Owned by ToolExecutor.

    Each run gets its own ``.tasks/`` directory under a run-scoped path,
    or shares a project-level directory (configurable).
    """

    def __init__(self, base_dir: str | Path = ".tasks") -> None:
        self._base_dir = Path(base_dir)
        self._managers: dict[str, TaskManager] = {}

    def get(self, run_id: str) -> TaskManager:
        """Get or create the TaskManager for a run.

        All runs share the same project-level tasks directory
        so tasks persist across runs.
        """
        if run_id not in self._managers:
            self._managers[run_id] = TaskManager(self._base_dir)
        return self._managers[run_id]

    def remove(self, run_id: str) -> None:
        """Release the manager reference (files remain on disk)."""
        self._managers.pop(run_id, None)


# ── Backward compat aliases ────────────────────────────────────────
# s03 code may reference these names
TodoStatus = TaskStatus
TodoManager = TaskManager
TodoService = TaskService

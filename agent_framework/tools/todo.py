"""Run-scoped TaskManager — persistent task graph with dependencies.

All tasks stored in a single ``tasks.json`` file (not one file per task).
Atomic read/write, easy to inspect, no glob scanning.

Features:
- DAG with ``blockedBy``/``blocks`` edges
- Completing a task auto-unblocks dependents
- Persisted to disk, survives compression and restart
- Configurable via ``TodoConfig`` (max_items, reminder_threshold)
- Single in_progress constraint enforced

Run-scoped via ``TaskService`` (maps run_id → TaskManager).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from enum import Enum
from typing import Any


# ── Models ─────────────────────────────────────────────────────────

class TaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    DELETED = "deleted"


# ── Constants ──────────────────────────────────────────────────────

_REMINDER_THRESHOLD = 3
_MAX_TASKS = 20


# ── TaskManager — single-file persistent DAG ──────────────────────

class TaskManager:
    """Persistent task graph backed by a single ``tasks.json`` file.

    File layout::

        .tasks/tasks.json
        {
          "next_id": 4,
          "tasks": {
            "1": {"id": 1, "subject": "...", "status": "completed", ...},
            "2": {"id": 2, "blockedBy": [1], ...}
          }
        }
    """

    def __init__(
        self,
        tasks_dir: str | Path = ".tasks",
        max_items: int = _MAX_TASKS,
        reminder_threshold: int = _REMINDER_THRESHOLD,
    ) -> None:
        self.dir = Path(tasks_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._file = self.dir / "tasks.json"
        self._max_items = max_items
        self._reminder_threshold = reminder_threshold
        self._rounds_since_write = 0
        # Migrate: if old per-file layout exists, convert it
        self._migrate_from_per_file()

    # ── CRUD ───────────────────────────────────────────────────────

    def create(
        self,
        subject: str,
        description: str = "",
        blocked_by: list[int] | None = None,
        active_form: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Create a new task. Returns JSON representation."""
        if not subject or not subject.strip():
            raise ValueError("subject is required and cannot be empty")

        store = self._load_store()
        if len(store["tasks"]) >= self._max_items:
            raise ValueError(
                f"Maximum {self._max_items} tasks allowed "
                f"(current: {len(store['tasks'])})"
            )

        task_id = store["next_id"]
        store["next_id"] = task_id + 1

        task: dict[str, Any] = {
            "id": task_id,
            "subject": subject,
            "description": description,
            "status": TaskStatus.PENDING.value,
            "blockedBy": blocked_by or [],
            "blocks": [],
            "owner": "",
            "activeForm": active_form,
            "metadata": metadata or {},
            "created_at": time.time(),
            "updated_at": time.time(),
        }

        # Register forward edges
        for dep_id in task["blockedBy"]:
            dep = store["tasks"].get(str(dep_id))
            if dep and task_id not in dep.get("blocks", []):
                dep.setdefault("blocks", []).append(task_id)

        store["tasks"][str(task_id)] = task
        self._save_store(store)
        self._rounds_since_write = 0
        return json.dumps(task, indent=2)

    def update(
        self,
        task_id: int,
        *,
        status: str | None = None,
        subject: str | None = None,
        description: str | None = None,
        active_form: str | None = None,
        add_blocked_by: list[int] | None = None,
        add_blocks: list[int] | None = None,
        owner: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Update an existing task. Returns updated JSON."""
        store = self._load_store()
        key = str(task_id)
        task = store["tasks"].get(key)
        if task is None:
            return json.dumps({"error": f"Task {task_id} not found"})

        # Handle deletion
        if status == TaskStatus.DELETED.value:
            self._delete_task_in_store(store, task_id)
            self._save_store(store)
            self._rounds_since_write = 0
            return json.dumps({"id": task_id, "status": "deleted"})

        # Validate status
        if status is not None and status not in {s.value for s in TaskStatus}:
            raise ValueError(
                f"Invalid status '{status}'. "
                f"Must be one of: pending, in_progress, completed, deleted"
            )

        # Single in_progress constraint
        if status == TaskStatus.IN_PROGRESS.value:
            for other in store["tasks"].values():
                if (other["status"] == "in_progress"
                        and other["id"] != task_id):
                    raise ValueError(
                        f"Only one task can be in_progress at a time. "
                        f"Task #{other['id']} is already in_progress. "
                        f"Complete or set it to pending first."
                    )

        if subject is not None:
            task["subject"] = subject
        if description is not None:
            task["description"] = description
        if active_form is not None:
            task["activeForm"] = active_form
        if owner is not None:
            task["owner"] = owner

        # Metadata merge — null deletes key
        if metadata is not None:
            existing = task.get("metadata", {})
            for k, v in metadata.items():
                if v is None:
                    existing.pop(k, None)
                else:
                    existing[k] = v
            task["metadata"] = existing

        # Add dependency edges
        if add_blocked_by:
            for dep_id in add_blocked_by:
                if dep_id not in task.get("blockedBy", []):
                    task.setdefault("blockedBy", []).append(dep_id)
                    dep = store["tasks"].get(str(dep_id))
                    if dep and task_id not in dep.get("blocks", []):
                        dep.setdefault("blocks", []).append(task_id)

        if add_blocks:
            for downstream_id in add_blocks:
                if downstream_id not in task.get("blocks", []):
                    task.setdefault("blocks", []).append(downstream_id)
                    downstream = store["tasks"].get(str(downstream_id))
                    if downstream and task_id not in downstream.get("blockedBy", []):
                        downstream.setdefault("blockedBy", []).append(task_id)

        # Status transition
        if status is not None:
            old_status = task.get("status")
            task["status"] = status
            if status == TaskStatus.COMPLETED.value and old_status != TaskStatus.COMPLETED.value:
                self._clear_dependency_in_store(store, task_id)

        task["updated_at"] = time.time()
        self._save_store(store)
        self._rounds_since_write = 0
        return json.dumps(task, indent=2)

    def get(self, task_id: int) -> str:
        """Get a single task by ID."""
        store = self._load_store()
        task = store["tasks"].get(str(task_id))
        if task is None:
            return json.dumps({"error": f"Task {task_id} not found"})
        return json.dumps(task, indent=2)

    def list_all(self) -> str:
        """List all tasks with summary."""
        store = self._load_store()
        tasks = sorted(store["tasks"].values(), key=lambda t: t.get("id", 0))

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

    @staticmethod
    def _clear_dependency_in_store(store: dict, completed_id: int) -> None:
        """Remove completed_id from every task's blockedBy."""
        for task in store["tasks"].values():
            blocked_by = task.get("blockedBy", [])
            if completed_id in blocked_by:
                blocked_by.remove(completed_id)

    @staticmethod
    def _delete_task_in_store(store: dict, task_id: int) -> None:
        """Remove a task and clean up all edges."""
        for task in store["tasks"].values():
            if task.get("id") == task_id:
                continue
            if task_id in task.get("blockedBy", []):
                task["blockedBy"].remove(task_id)
            if task_id in task.get("blocks", []):
                task["blocks"].remove(task_id)
        store["tasks"].pop(str(task_id), None)

    # ── Round tracking ─────────────────────────────────────────────

    def mark_round(self, wrote_task: bool) -> None:
        if wrote_task:
            self._rounds_since_write = 0
        else:
            self._rounds_since_write += 1

    def should_remind(self) -> bool:
        return self.has_tasks and self._rounds_since_write >= self._reminder_threshold

    @property
    def rounds_since_write(self) -> int:
        return self._rounds_since_write

    @property
    def has_tasks(self) -> bool:
        if not self._file.exists():
            return False
        store = self._load_store()
        return bool(store["tasks"])

    # ── Summary ────────────────────────────────────────────────────

    def summary_text(self) -> str:
        if not self.has_tasks:
            return ""
        store = self._load_store()
        tasks = list(store["tasks"].values())
        total = len(tasks)
        completed = sum(1 for t in tasks if t["status"] == "completed")
        in_prog = sum(1 for t in tasks if t["status"] == "in_progress")
        ready = sum(1 for t in tasks if t["status"] == "pending" and not t.get("blockedBy"))
        blocked = sum(1 for t in tasks if t["status"] == "pending" and t.get("blockedBy"))
        parts = [f"{completed}/{total} done"]
        if in_prog:
            parts.append(f"{in_prog} active")
        if ready:
            parts.append(f"{ready} ready")
        if blocked:
            parts.append(f"{blocked} blocked")
        return ", ".join(parts)

    # ── File I/O (single file) ─────────────────────────────────────

    def _load_store(self) -> dict:
        """Load the tasks.json store. Returns default if missing/corrupt."""
        if not self._file.exists():
            return {"next_id": 1, "tasks": {}}
        try:
            data = json.loads(self._file.read_text())
            if "tasks" not in data:
                return {"next_id": 1, "tasks": {}}
            return data
        except (json.JSONDecodeError, OSError):
            return {"next_id": 1, "tasks": {}}

    def _save_store(self, store: dict) -> None:
        """Atomic write: write to temp then rename."""
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(store, indent=2, ensure_ascii=False))
        tmp.rename(self._file)

    # ── Migration from per-file layout ─────────────────────────────

    def _migrate_from_per_file(self) -> None:
        """One-time migration: convert task_N.json files → tasks.json."""
        per_files = list(self.dir.glob("task_*.json"))
        if not per_files or self._file.exists():
            return
        store: dict[str, Any] = {"next_id": 1, "tasks": {}}
        max_id = 0
        for path in sorted(per_files):
            try:
                task = json.loads(path.read_text())
                tid = int(task.get("id", 0))
                store["tasks"][str(tid)] = task
                max_id = max(max_id, tid)
            except (json.JSONDecodeError, OSError, ValueError):
                continue
        store["next_id"] = max_id + 1
        self._save_store(store)
        # Clean up old files
        for path in per_files:
            path.unlink(missing_ok=True)


# ── TaskService — run-scoped registry ──────────────────────────────

class TaskService:
    """Maps run_id → TaskManager. Owned by ToolExecutor."""

    def __init__(
        self,
        base_dir: str | Path = ".tasks",
        max_items: int = _MAX_TASKS,
        reminder_threshold: int = _REMINDER_THRESHOLD,
    ) -> None:
        self._base_dir = Path(base_dir)
        self._max_items = max_items
        self._reminder_threshold = reminder_threshold
        self._managers: dict[str, TaskManager] = {}

    def get(self, run_id: str) -> TaskManager:
        if run_id not in self._managers:
            self._managers[run_id] = TaskManager(
                self._base_dir, self._max_items, self._reminder_threshold,
            )
        return self._managers[run_id]

    def remove(self, run_id: str) -> None:
        self._managers.pop(run_id, None)


# ── Backward compat aliases ────────────────────────────────────────
TodoStatus = TaskStatus
TodoManager = TaskManager
TodoService = TaskService

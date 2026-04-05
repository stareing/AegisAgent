"""Background process registry — tracks long-running tool processes.

Provides poll, log, kill, and cleanup operations for background
shell processes. Each process gets a unique ID and TTL-based cleanup.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class ProcessRecord(BaseModel):
    """Tracking record for a background process."""

    process_id: str
    pid: int = 0
    command: str = ""
    cwd: str = ""
    started_at: float = Field(default_factory=time.monotonic)
    finished_at: float | None = None
    exit_code: int | None = None
    status: str = "running"  # "running" | "completed" | "failed" | "killed"
    output_lines: list[str] = Field(default_factory=list)
    scope_key: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status in ("completed", "failed", "killed")

    @property
    def duration_seconds(self) -> float:
        end = self.finished_at or time.monotonic()
        return end - self.started_at


class ProcessRegistry:
    """In-memory registry for background processes with TTL cleanup."""

    # Default TTL for finished processes before garbage collection
    DEFAULT_CLEANUP_TTL_SECONDS = 300  # 5 minutes

    def __init__(self, cleanup_ttl: float | None = None) -> None:
        self._records: dict[str, ProcessRecord] = {}
        self._cleanup_ttl = cleanup_ttl or self.DEFAULT_CLEANUP_TTL_SECONDS
        self._counter = 0

    def register(
        self,
        pid: int,
        command: str,
        cwd: str = "",
        scope_key: str = "",
    ) -> str:
        """Register a new process and return its ID."""
        self._counter += 1
        process_id = f"proc-{self._counter}"

        record = ProcessRecord(
            process_id=process_id,
            pid=pid,
            command=command,
            cwd=cwd,
            scope_key=scope_key,
        )
        self._records[process_id] = record
        logger.info(
            "process_registry.registered",
            process_id=process_id,
            pid=pid,
            command=command[:200],
        )
        return process_id

    def mark_completed(
        self,
        process_id: str,
        exit_code: int,
        output_lines: list[str] | None = None,
    ) -> None:
        """Mark a process as completed."""
        record = self._records.get(process_id)
        if not record:
            return

        record.finished_at = time.monotonic()
        record.exit_code = exit_code
        record.status = "completed" if exit_code == 0 else "failed"
        if output_lines is not None:
            record.output_lines = output_lines

    def mark_killed(self, process_id: str) -> None:
        """Mark a process as killed."""
        record = self._records.get(process_id)
        if not record:
            return
        record.finished_at = time.monotonic()
        record.status = "killed"

    def get(self, process_id: str) -> ProcessRecord | None:
        return self._records.get(process_id)

    def append_output(self, process_id: str, line: str) -> None:
        """Append a line to process output."""
        record = self._records.get(process_id)
        if record:
            record.output_lines.append(line)

    def get_output(
        self,
        process_id: str,
        offset: int = 0,
        limit: int = 200,
    ) -> list[str]:
        """Get paginated output lines."""
        record = self._records.get(process_id)
        if not record:
            return []
        return record.output_lines[offset : offset + limit]

    def list_active(self, scope_key: str = "") -> list[ProcessRecord]:
        """List all running processes, optionally filtered by scope."""
        return [
            r for r in self._records.values()
            if r.status == "running"
            and (not scope_key or r.scope_key == scope_key)
        ]

    def list_all(self, scope_key: str = "") -> list[ProcessRecord]:
        """List all processes (running + finished)."""
        return [
            r for r in self._records.values()
            if not scope_key or r.scope_key == scope_key
        ]

    def cleanup_expired(self) -> int:
        """Remove finished processes older than TTL. Returns count removed."""
        now = time.monotonic()
        expired = [
            pid for pid, record in self._records.items()
            if record.is_terminal
            and record.finished_at is not None
            and (now - record.finished_at) > self._cleanup_ttl
        ]
        for pid in expired:
            del self._records[pid]

        if expired:
            logger.info(
                "process_registry.cleanup",
                removed_count=len(expired),
            )
        return len(expired)

    def clear(self) -> None:
        """Remove all records."""
        self._records.clear()

    @property
    def count(self) -> int:
        return len(self._records)

    @property
    def active_count(self) -> int:
        return sum(1 for r in self._records.values() if r.status == "running")

"""Cron scheduler — manages scheduled agent task execution.

Persists cron jobs and triggers agent runs at scheduled times.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine

from agent_framework.infra.logger import get_logger
from agent_framework.scheduling.cron_parser import CronExpression, next_run, parse_cron

logger = get_logger(__name__)


@dataclass
class CronJob:
    """A scheduled cron job definition."""

    job_id: str
    name: str
    cron_expression: str
    task_prompt: str
    enabled: bool = True
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    last_run_at: str | None = None
    next_run_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CronRegistry:
    """SQLite-backed persistent cron job registry."""

    def __init__(self, db_path: str = "data/cron_jobs.db") -> None:
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cron_jobs (
                    job_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    cron_expression TEXT NOT NULL,
                    task_prompt TEXT NOT NULL,
                    enabled INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    metadata TEXT DEFAULT '{}'
                )
            """)

    def create(
        self,
        name: str,
        cron_expression: str,
        task_prompt: str,
        metadata: dict | None = None,
    ) -> CronJob:
        """Create and persist a new cron job."""
        # Validate cron expression
        cron = parse_cron(cron_expression)
        next_dt = next_run(cron)

        job = CronJob(
            job_id=str(uuid.uuid4()),
            name=name,
            cron_expression=cron_expression,
            task_prompt=task_prompt,
            next_run_at=next_dt.isoformat() if next_dt else None,
            metadata=metadata or {},
        )

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO cron_jobs
                   (job_id, name, cron_expression, task_prompt, enabled,
                    created_at, next_run_at, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id, job.name, job.cron_expression,
                    job.task_prompt, 1, job.created_at,
                    job.next_run_at, json.dumps(job.metadata),
                ),
            )

        logger.info(
            "cron.job_created",
            job_id=job.job_id,
            name=name,
            expression=cron_expression,
            next_run=job.next_run_at,
        )
        return job

    def delete(self, job_id: str) -> bool:
        """Delete a cron job. Returns True if found."""
        with sqlite3.connect(self._db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM cron_jobs WHERE job_id = ?", (job_id,)
            )
            return cursor.rowcount > 0

    def list_jobs(self, enabled_only: bool = False) -> list[CronJob]:
        """List all cron jobs."""
        query = "SELECT * FROM cron_jobs"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY created_at"

        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(query).fetchall()

        return [
            CronJob(
                job_id=r["job_id"],
                name=r["name"],
                cron_expression=r["cron_expression"],
                task_prompt=r["task_prompt"],
                enabled=bool(r["enabled"]),
                created_at=r["created_at"],
                last_run_at=r["last_run_at"],
                next_run_at=r["next_run_at"],
                metadata=json.loads(r["metadata"] or "{}"),
            )
            for r in rows
        ]

    def get(self, job_id: str) -> CronJob | None:
        """Get a cron job by ID."""
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM cron_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()

        if not row:
            return None

        return CronJob(
            job_id=row["job_id"],
            name=row["name"],
            cron_expression=row["cron_expression"],
            task_prompt=row["task_prompt"],
            enabled=bool(row["enabled"]),
            created_at=row["created_at"],
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def mark_executed(self, job_id: str) -> None:
        """Update last_run_at and compute next_run_at after execution."""
        job = self.get(job_id)
        if not job:
            return

        now = datetime.now(timezone.utc)
        cron = parse_cron(job.cron_expression)
        next_dt = next_run(cron, after=now)

        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """UPDATE cron_jobs
                   SET last_run_at = ?, next_run_at = ?
                   WHERE job_id = ?""",
                (
                    now.isoformat(),
                    next_dt.isoformat() if next_dt else None,
                    job_id,
                ),
            )


class CronScheduler:
    """Background scheduler that checks and triggers cron jobs.

    Runs as an asyncio task, checking every minute for due jobs.
    """

    def __init__(
        self,
        registry: CronRegistry,
        run_callback: Callable[[str, str], Coroutine] | None = None,
        check_interval_seconds: float = 60.0,
    ) -> None:
        self._registry = registry
        self._run_callback = run_callback
        self._interval = check_interval_seconds
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the scheduler background loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("cron.scheduler_started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("cron.scheduler_stopped")

    async def _loop(self) -> None:
        """Main scheduler loop."""
        while self._running:
            try:
                await self._check_and_trigger()
            except Exception as e:
                logger.warning("cron.check_error", error=str(e))
            await asyncio.sleep(self._interval)

    async def _check_and_trigger(self) -> None:
        """Check all enabled jobs and trigger those that are due."""
        now = datetime.now(timezone.utc)
        jobs = self._registry.list_jobs(enabled_only=True)

        for job in jobs:
            if not job.next_run_at:
                continue

            next_dt = datetime.fromisoformat(job.next_run_at)
            if next_dt <= now:
                logger.info(
                    "cron.triggering",
                    job_id=job.job_id,
                    name=job.name,
                )
                self._registry.mark_executed(job.job_id)
                if self._run_callback:
                    try:
                        await self._run_callback(job.job_id, job.task_prompt)
                    except Exception as e:
                        logger.warning(
                            "cron.trigger_failed",
                            job_id=job.job_id,
                            error=str(e),
                        )

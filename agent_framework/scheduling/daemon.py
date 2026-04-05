"""Cron execution daemon — triggers agent runs at scheduled times.

Bridges the existing CronScheduler (which has _check_and_trigger but was
never started) with the framework's run callback. Adds job lifecycle
management: max age cleanup, job count limits, durable vs session-only.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Callable, Coroutine

from agent_framework.infra.logger import get_logger
from agent_framework.scheduling.scheduler import CronRegistry, CronScheduler

logger = get_logger(__name__)

# Limits
MAX_JOBS = 50
DEFAULT_MAX_AGE_DAYS = 30
DEFAULT_CHECK_INTERVAL = 60.0  # seconds


class CronDaemon:
    """Manages CronScheduler lifecycle and job housekeeping.

    Responsibilities:
    - Start/stop the scheduler background loop
    - Enforce MAX_JOBS limit on creation
    - Cleanup expired jobs (older than max_age_days)
    - Track durable vs session-only jobs
    """

    def __init__(
        self,
        registry: CronRegistry,
        run_callback: Callable[[str, str], Coroutine] | None = None,
        *,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL,
        max_jobs: int = MAX_JOBS,
        max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    ) -> None:
        self._registry = registry
        self._max_jobs = max_jobs
        self._max_age_days = max_age_days
        self._scheduler = CronScheduler(
            registry=registry,
            run_callback=run_callback,
            check_interval_seconds=check_interval_seconds,
        )
        self._running = False
        # Session-only job IDs — deleted on stop()
        self._session_jobs: set[str] = set()

    @property
    def registry(self) -> CronRegistry:
        return self._registry

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Start the daemon: cleanup expired jobs, then start scheduler."""
        if self._running:
            return

        self.cleanup_expired()
        await self._scheduler.start()
        self._running = True
        logger.info("cron_daemon.started", max_jobs=self._max_jobs)

    async def stop(self) -> None:
        """Stop the daemon and remove session-only jobs."""
        if not self._running:
            return

        await self._scheduler.stop()
        self._running = False

        # Remove session-only jobs
        removed = 0
        for job_id in list(self._session_jobs):
            if self._registry.delete(job_id):
                removed += 1
        self._session_jobs.clear()

        logger.info("cron_daemon.stopped", session_jobs_removed=removed)

    def can_create_job(self) -> bool:
        """Check if a new job can be created (under MAX_JOBS limit)."""
        return len(self._registry.list_jobs()) < self._max_jobs

    def create_job(
        self,
        name: str,
        cron_expression: str,
        task_prompt: str,
        *,
        durable: bool = False,
        metadata: dict | None = None,
    ) -> dict:
        """Create a job with limit enforcement and type tracking.

        Returns dict with job info or error.
        """
        if not self.can_create_job():
            return {
                "error": f"Maximum job limit reached ({self._max_jobs}). "
                "Delete existing jobs before creating new ones.",
            }

        job = self._registry.create(
            name=name,
            cron_expression=cron_expression,
            task_prompt=task_prompt,
            metadata={**(metadata or {}), "job_type": "durable" if durable else "session"},
        )

        if not durable:
            self._session_jobs.add(job.job_id)

        return {
            "job_id": job.job_id,
            "name": job.name,
            "cron_expression": job.cron_expression,
            "next_run_at": job.next_run_at,
            "job_type": "durable" if durable else "session",
        }

    def cleanup_expired(self) -> int:
        """Remove jobs older than max_age_days. Returns count removed."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._max_age_days)
        jobs = self._registry.list_jobs()
        removed = 0

        for job in jobs:
            try:
                created = datetime.fromisoformat(job.created_at)
                if created < cutoff:
                    self._registry.delete(job.job_id)
                    self._session_jobs.discard(job.job_id)
                    removed += 1
            except (ValueError, TypeError):
                continue

        if removed > 0:
            logger.info("cron_daemon.cleanup_expired", removed=removed)
        return removed

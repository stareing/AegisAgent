"""Tests for v5.0 cron execution daemon."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.scheduling.daemon import (
    DEFAULT_CHECK_INTERVAL,
    DEFAULT_MAX_AGE_DAYS,
    MAX_JOBS,
    CronDaemon,
)
from agent_framework.scheduling.scheduler import CronJob, CronRegistry


@pytest.fixture
def tmp_db(tmp_path):
    return str(tmp_path / "test_cron.db")


@pytest.fixture
def registry(tmp_db):
    return CronRegistry(db_path=tmp_db)


@pytest.fixture
def daemon(registry):
    return CronDaemon(
        registry=registry,
        run_callback=AsyncMock(),
        max_jobs=5,
        max_age_days=30,
    )


# ===========================================================================
# Lifecycle
# ===========================================================================


class TestDaemonLifecycle:

    @pytest.mark.asyncio
    async def test_start_stop(self, daemon):
        await daemon.start()
        assert daemon.is_running is True
        await daemon.stop()
        assert daemon.is_running is False

    @pytest.mark.asyncio
    async def test_double_start(self, daemon):
        await daemon.start()
        await daemon.start()  # should be idempotent
        assert daemon.is_running is True
        await daemon.stop()

    @pytest.mark.asyncio
    async def test_stop_removes_session_jobs(self, daemon):
        result = daemon.create_job(
            "test", "*/5 * * * *", "do thing", durable=False,
        )
        assert "job_id" in result
        job_id = result["job_id"]

        await daemon.start()
        await daemon.stop()

        # Session job should be deleted
        job = daemon.registry.get(job_id)
        assert job is None


# ===========================================================================
# Job Creation
# ===========================================================================


class TestJobCreation:

    def test_create_durable(self, daemon):
        result = daemon.create_job(
            "durable-task", "0 9 * * 1", "weekly task", durable=True,
        )
        assert result["job_type"] == "durable"
        assert "job_id" in result

    def test_create_session(self, daemon):
        result = daemon.create_job(
            "session-task", "*/5 * * * *", "frequent task", durable=False,
        )
        assert result["job_type"] == "session"

    def test_max_jobs_limit(self, daemon):
        # daemon has max_jobs=5
        for i in range(5):
            result = daemon.create_job(f"job-{i}", "*/5 * * * *", f"task {i}")
            assert "job_id" in result

        # 6th should fail
        result = daemon.create_job("job-6", "*/5 * * * *", "too many")
        assert "error" in result

    def test_can_create_job(self, daemon):
        assert daemon.can_create_job() is True
        for i in range(5):
            daemon.create_job(f"job-{i}", "*/5 * * * *", f"task {i}")
        assert daemon.can_create_job() is False


# ===========================================================================
# Expiry Cleanup
# ===========================================================================


class TestCleanupExpired:

    def test_removes_old_jobs(self, registry):
        daemon = CronDaemon(registry=registry, max_age_days=1)

        # Create a job then manually set created_at to old date
        job = registry.create("old", "*/5 * * * *", "old task")
        import sqlite3
        old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
        with sqlite3.connect(registry._db_path) as conn:
            conn.execute(
                "UPDATE cron_jobs SET created_at = ? WHERE job_id = ?",
                (old_date, job.job_id),
            )

        removed = daemon.cleanup_expired()
        assert removed == 1
        assert registry.get(job.job_id) is None

    def test_keeps_recent_jobs(self, registry):
        daemon = CronDaemon(registry=registry, max_age_days=30)
        registry.create("recent", "*/5 * * * *", "recent task")
        removed = daemon.cleanup_expired()
        assert removed == 0


# ===========================================================================
# Defaults
# ===========================================================================


class TestDefaults:

    def test_max_jobs_constant(self):
        assert MAX_JOBS == 50

    def test_max_age_days_constant(self):
        assert DEFAULT_MAX_AGE_DAYS == 30

    def test_check_interval_constant(self):
        assert DEFAULT_CHECK_INTERVAL == 60.0

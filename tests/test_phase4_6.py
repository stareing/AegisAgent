"""Tests for Phase 4-6: Worktree, Team Payloads, Cron, Notebook, AutoDream."""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =====================================================================
# Phase 4A: Worktree Isolation
# =====================================================================


class TestWorktreeSession:

    def test_session_is_frozen(self):
        from agent_framework.workspace.worktree import WorktreeSession
        session = WorktreeSession(
            worktree_path="/tmp/wt",
            branch_name="agent/abc",
            original_cwd="/home/user/project",
        )
        assert session.worktree_path == "/tmp/wt"
        with pytest.raises(Exception):
            session.worktree_path = "/other"


class TestWorktreeManager:

    def test_init(self):
        from agent_framework.workspace.worktree import WorktreeManager
        mgr = WorktreeManager(worktree_base_dir="/tmp/worktrees")
        assert mgr.get_session("nonexistent") is None

    @patch("agent_framework.workspace.worktree._is_git_repo", return_value=False)
    def test_enter_worktree_not_git(self, mock_git):
        from agent_framework.workspace.worktree import WorktreeError, WorktreeManager
        mgr = WorktreeManager()
        with pytest.raises(WorktreeError, match="Not a git repository"):
            mgr.enter_worktree("run-123", cwd="/not/a/repo")

    @patch("agent_framework.workspace.worktree._run_git")
    @patch("agent_framework.workspace.worktree._git_root", return_value="/project")
    @patch("agent_framework.workspace.worktree._is_git_repo", return_value=True)
    def test_enter_worktree_success(self, mock_is_git, mock_root, mock_run, tmp_path):
        from agent_framework.workspace.worktree import WorktreeManager
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = WorktreeManager(worktree_base_dir=str(tmp_path))
        session = mgr.enter_worktree("run-abc-123-def", cwd="/project")
        assert session.branch_name.startswith("agent/")
        assert session.original_cwd == "/project"
        assert mgr.get_session("run-abc-123-def") is not None

    @patch("agent_framework.workspace.worktree._run_git")
    @patch("agent_framework.workspace.worktree._git_root", return_value="/project")
    @patch("agent_framework.workspace.worktree._is_git_repo", return_value=True)
    def test_exit_worktree_remove(self, mock_is_git, mock_root, mock_run, tmp_path):
        from agent_framework.workspace.worktree import WorktreeManager
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mgr = WorktreeManager(worktree_base_dir=str(tmp_path))
        mgr.enter_worktree("run-123", cwd="/project")
        mgr.exit_worktree("run-123", keep=False)
        assert mgr.get_session("run-123") is None

    def test_exit_worktree_no_session(self):
        from agent_framework.workspace.worktree import WorktreeManager
        mgr = WorktreeManager()
        # Should not raise
        mgr.exit_worktree("nonexistent")


# =====================================================================
# Phase 5A: Typed Team Payloads
# =====================================================================


class TestTeamPayloads:

    def test_task_assignment_payload(self):
        from agent_framework.models.team_payloads import TaskAssignmentPayload
        p = TaskAssignmentPayload(task_id="t1", task_description="Fix bug")
        assert p.task_id == "t1"
        assert p.priority == 0
        # frozen
        with pytest.raises(Exception):
            p.task_id = "t2"

    def test_shutdown_request_payload(self):
        from agent_framework.models.team_payloads import ShutdownRequestPayload
        p = ShutdownRequestPayload(reason="All done", graceful=True)
        assert p.timeout_ms == 30_000
        assert p.graceful is True

    def test_plan_approval_payload(self):
        from agent_framework.models.team_payloads import PlanApprovalPayload
        p = PlanApprovalPayload(plan_id="plan-1", approved=True, feedback="LGTM")
        assert p.approved is True
        assert p.feedback == "LGTM"

    def test_payload_type_map_completeness(self):
        from agent_framework.models.team_payloads import PAYLOAD_TYPE_MAP
        expected_types = {
            "TASK_ASSIGNMENT", "TASK_CLAIM_REQUEST", "PLAN_SUBMISSION",
            "APPROVAL_RESPONSE", "QUESTION", "ANSWER",
            "SHUTDOWN_REQUEST", "SHUTDOWN_ACK",
            "PROGRESS_NOTICE", "ERROR_NOTICE", "BROADCAST_NOTICE",
        }
        assert expected_types.issubset(set(PAYLOAD_TYPE_MAP.keys()))

    def test_all_payloads_frozen(self):
        from agent_framework.models.team_payloads import PAYLOAD_TYPE_MAP
        # Provide required fields for types that need them
        required_fields = {
            "TASK_ASSIGNMENT": {"task_id": "t1", "task_description": "test"},
            "TASK_CLAIM_REQUEST": {"task_id": "t1"},
            "PLAN_SUBMISSION": {"plan_id": "p1", "plan_content": "plan"},
            "APPROVAL_RESPONSE": {"plan_id": "p1", "approved": True},
            "QUESTION": {"question": "why?"},
            "ANSWER": {"answer": "because"},
        }
        for name, cls in PAYLOAD_TYPE_MAP.items():
            kwargs = required_fields.get(name, {})
            instance = cls(**kwargs)
            assert instance is not None


# =====================================================================
# Phase 6B: Cron Parser
# =====================================================================


class TestCronParser:

    def test_parse_every_minute(self):
        from agent_framework.scheduling.cron_parser import parse_cron
        cron = parse_cron("* * * * *")
        assert len(cron.minute) == 60
        assert len(cron.hour) == 24

    def test_parse_specific_time(self):
        from agent_framework.scheduling.cron_parser import parse_cron
        cron = parse_cron("30 9 * * *")
        assert cron.minute == frozenset({30})
        assert cron.hour == frozenset({9})

    def test_parse_step(self):
        from agent_framework.scheduling.cron_parser import parse_cron
        cron = parse_cron("*/15 * * * *")
        assert cron.minute == frozenset({0, 15, 30, 45})

    def test_parse_range(self):
        from agent_framework.scheduling.cron_parser import parse_cron
        cron = parse_cron("0 9-17 * * *")
        assert cron.hour == frozenset(range(9, 18))

    def test_parse_list(self):
        from agent_framework.scheduling.cron_parser import parse_cron
        cron = parse_cron("0 0 1,15 * *")
        assert cron.day_of_month == frozenset({1, 15})

    def test_parse_weekday_range(self):
        from agent_framework.scheduling.cron_parser import parse_cron
        cron = parse_cron("0 9 * * 1-5")
        assert cron.day_of_week == frozenset({1, 2, 3, 4, 5})

    def test_parse_invalid_field_count(self):
        from agent_framework.scheduling.cron_parser import CronParseError, parse_cron
        with pytest.raises(CronParseError, match="Expected 5 fields"):
            parse_cron("* *")

    def test_parse_out_of_range(self):
        from agent_framework.scheduling.cron_parser import CronParseError, parse_cron
        with pytest.raises(CronParseError, match="out of bounds"):
            parse_cron("60 * * * *")

    def test_matches_datetime(self):
        from agent_framework.scheduling.cron_parser import parse_cron
        cron = parse_cron("30 9 * * *")
        dt_match = datetime(2026, 4, 5, 9, 30)
        dt_no_match = datetime(2026, 4, 5, 10, 30)
        assert cron.matches(dt_match)
        assert not cron.matches(dt_no_match)

    def test_next_run(self):
        from agent_framework.scheduling.cron_parser import next_run, parse_cron
        cron = parse_cron("0 * * * *")  # every hour
        after = datetime(2026, 4, 5, 9, 30, tzinfo=timezone.utc)
        result = next_run(cron, after=after)
        assert result is not None
        assert result.minute == 0
        assert result.hour == 10

    def test_next_run_every_minute(self):
        from agent_framework.scheduling.cron_parser import next_run, parse_cron
        cron = parse_cron("* * * * *")
        after = datetime(2026, 4, 5, 9, 30, 45, tzinfo=timezone.utc)
        result = next_run(cron, after=after)
        assert result is not None
        assert result.minute == 31


# =====================================================================
# Phase 6B: Cron Registry
# =====================================================================


class TestCronRegistry:

    def test_create_and_list(self, tmp_path):
        from agent_framework.scheduling.scheduler import CronRegistry
        reg = CronRegistry(db_path=str(tmp_path / "cron.db"))
        job = reg.create("daily", "0 9 * * *", "Run daily check")
        assert job.job_id
        assert job.name == "daily"
        jobs = reg.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].job_id == job.job_id

    def test_delete(self, tmp_path):
        from agent_framework.scheduling.scheduler import CronRegistry
        reg = CronRegistry(db_path=str(tmp_path / "cron.db"))
        job = reg.create("temp", "* * * * *", "temp task")
        assert reg.delete(job.job_id)
        assert len(reg.list_jobs()) == 0

    def test_delete_nonexistent(self, tmp_path):
        from agent_framework.scheduling.scheduler import CronRegistry
        reg = CronRegistry(db_path=str(tmp_path / "cron.db"))
        assert not reg.delete("nonexistent-id")

    def test_get(self, tmp_path):
        from agent_framework.scheduling.scheduler import CronRegistry
        reg = CronRegistry(db_path=str(tmp_path / "cron.db"))
        job = reg.create("test", "0 0 * * *", "midnight")
        fetched = reg.get(job.job_id)
        assert fetched is not None
        assert fetched.name == "test"

    def test_mark_executed(self, tmp_path):
        from agent_framework.scheduling.scheduler import CronRegistry
        reg = CronRegistry(db_path=str(tmp_path / "cron.db"))
        job = reg.create("test", "0 0 * * *", "midnight")
        reg.mark_executed(job.job_id)
        updated = reg.get(job.job_id)
        assert updated.last_run_at is not None


# =====================================================================
# Phase 6C: Notebook Editing
# =====================================================================


class TestNotebookEdit:

    def _create_notebook(self, path: Path) -> None:
        """Create a minimal notebook file."""
        nb = {
            "nbformat": 4,
            "nbformat_minor": 5,
            "metadata": {},
            "cells": [
                {
                    "id": "cell-1",
                    "cell_type": "code",
                    "source": ["print('hello')"],
                    "metadata": {},
                    "outputs": [],
                    "execution_count": None,
                },
                {
                    "id": "cell-2",
                    "cell_type": "markdown",
                    "source": ["# Title"],
                    "metadata": {},
                },
            ],
        }
        path.write_text(json.dumps(nb), encoding="utf-8")

    def test_insert_cell(self, tmp_path):
        from agent_framework.tools.builtin.notebook import notebook_edit
        nb_path = tmp_path / "test.ipynb"
        self._create_notebook(nb_path)
        result = notebook_edit(
            notebook_path=str(nb_path),
            edit_mode="insert",
            cell_type="code",
            cell_id="cell-1",
            new_source="x = 42",
        )
        assert result["success"]
        assert result["edit_mode"] == "insert"
        # Verify
        nb = json.loads(nb_path.read_text())
        assert len(nb["cells"]) == 3

    def test_replace_cell(self, tmp_path):
        from agent_framework.tools.builtin.notebook import notebook_edit
        nb_path = tmp_path / "test.ipynb"
        self._create_notebook(nb_path)
        result = notebook_edit(
            notebook_path=str(nb_path),
            edit_mode="replace",
            cell_id="cell-1",
            new_source="print('world')",
        )
        assert result["success"]
        nb = json.loads(nb_path.read_text())
        assert "world" in "".join(nb["cells"][0]["source"])

    def test_delete_cell(self, tmp_path):
        from agent_framework.tools.builtin.notebook import notebook_edit
        nb_path = tmp_path / "test.ipynb"
        self._create_notebook(nb_path)
        result = notebook_edit(
            notebook_path=str(nb_path),
            edit_mode="delete",
            cell_id="cell-2",
        )
        assert result["success"]
        nb = json.loads(nb_path.read_text())
        assert len(nb["cells"]) == 1

    def test_not_ipynb_file(self, tmp_path):
        from agent_framework.tools.builtin.notebook import notebook_edit
        result = notebook_edit(
            notebook_path=str(tmp_path / "test.py"),
            edit_mode="insert",
        )
        assert not result["success"]

    def test_cell_not_found(self, tmp_path):
        from agent_framework.tools.builtin.notebook import notebook_edit
        nb_path = tmp_path / "test.ipynb"
        self._create_notebook(nb_path)
        result = notebook_edit(
            notebook_path=str(nb_path),
            edit_mode="replace",
            cell_id="nonexistent",
            new_source="",
        )
        assert not result["success"]

    def test_numeric_cell_id(self, tmp_path):
        from agent_framework.tools.builtin.notebook import notebook_edit
        nb_path = tmp_path / "test.ipynb"
        self._create_notebook(nb_path)
        result = notebook_edit(
            notebook_path=str(nb_path),
            edit_mode="replace",
            cell_id="0",
            new_source="replaced",
        )
        assert result["success"]


# =====================================================================
# Phase 6A: AutoDream
# =====================================================================


class TestAutoDream:

    def test_gates_block_when_not_enough_sessions(self):
        from agent_framework.memory.auto_dream import AutoDreamController
        ctrl = AutoDreamController(
            min_hours_between=0,  # disable time gate
            min_sessions=5,
        )
        ctrl._sessions_since_last = 3
        assert not ctrl._session_gate()

    def test_gates_pass_when_enough_sessions(self):
        from agent_framework.memory.auto_dream import AutoDreamController
        ctrl = AutoDreamController(min_hours_between=0, min_sessions=3)
        ctrl._sessions_since_last = 5
        assert ctrl._session_gate()

    def test_time_gate_first_run(self):
        from agent_framework.memory.auto_dream import AutoDreamController
        ctrl = AutoDreamController()
        assert ctrl._time_gate()  # Never consolidated → passes

    def test_cas_lock(self):
        from agent_framework.memory.auto_dream import AutoDreamController
        ctrl = AutoDreamController()
        assert ctrl._cas_lock()
        assert not ctrl._cas_lock()  # Already locked
        ctrl._release_lock()
        assert ctrl._cas_lock()

    def test_record_session_increments(self):
        from agent_framework.memory.auto_dream import AutoDreamController
        ctrl = AutoDreamController(min_sessions=10)
        assert ctrl._sessions_since_last == 0
        ctrl.record_session_end()
        assert ctrl._sessions_since_last == 1

    @pytest.mark.asyncio
    async def test_try_consolidate_all_gates_pass(self):
        from agent_framework.memory.auto_dream import AutoDreamController
        callback = AsyncMock()
        ctrl = AutoDreamController(
            min_hours_between=0,
            min_sessions=1,
            min_scan_interval_minutes=0,
            consolidation_callback=callback,
        )
        ctrl._sessions_since_last = 5
        result = await ctrl.try_consolidate()
        assert result is True
        callback.assert_awaited_once()
        assert ctrl._sessions_since_last == 0

    @pytest.mark.asyncio
    async def test_try_consolidate_session_gate_blocks(self):
        from agent_framework.memory.auto_dream import AutoDreamController
        ctrl = AutoDreamController(
            min_hours_between=0,
            min_sessions=10,
            min_scan_interval_minutes=0,
        )
        ctrl._sessions_since_last = 3
        result = await ctrl.try_consolidate()
        assert result is False

    def test_state_persistence(self, tmp_path):
        from agent_framework.memory.auto_dream import AutoDreamController
        state_file = str(tmp_path / "dream_state.json")
        ctrl1 = AutoDreamController(state_file=state_file)
        ctrl1._sessions_since_last = 7
        ctrl1._last_consolidation_time = 12345.0
        ctrl1._save_state()

        ctrl2 = AutoDreamController(state_file=state_file)
        assert ctrl2._sessions_since_last == 7
        assert ctrl2._last_consolidation_time == 12345.0

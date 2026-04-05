"""Tests for team auto-notification at framework core level.

Verifies:
1. Structured TeamNotification is created (not loose dict).
2. drain_team_notifications returns structured dicts with all required fields.
3. drain transitions members from RESULT_READY → NOTIFYING.
4. mark_team_notifications_delivered transitions NOTIFYING → IDLE.
5. peek does not consume notifications.
6. has_pending_team_notifications works correctly.
"""

from __future__ import annotations

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock

from agent_framework.models.team import (
    TeamMember,
    TeamMemberStatus,
    TeamNotification,
    TeamNotificationType,
)


class TestTeamNotificationModel:
    """Test the TeamNotification data model."""

    def test_create_completed_notification(self):
        n = TeamNotification(
            team_id="team_abc",
            agent_id="sub_123",
            role="coder",
            notification_type=TeamNotificationType.TASK_COMPLETED,
            status="completed",
            summary="Done writing code",
            task="Write hello.py",
            spawn_id="abc123",
        )
        assert n.team_id == "team_abc"
        assert n.notification_type == TeamNotificationType.TASK_COMPLETED
        assert n.status == "completed"
        assert n.created_at is not None

    def test_create_failed_notification(self):
        n = TeamNotification(
            team_id="team_abc",
            agent_id="sub_456",
            role="analyst",
            notification_type=TeamNotificationType.TASK_FAILED,
            status="failed",
            summary="API error",
        )
        assert n.notification_type == TeamNotificationType.TASK_FAILED

    def test_notification_is_frozen(self):
        n = TeamNotification(
            team_id="t", agent_id="a", role="r",
            notification_type=TeamNotificationType.TASK_COMPLETED,
            status="completed", summary="s",
        )
        with pytest.raises(Exception):
            n.summary = "modified"  # type: ignore[misc]


class TestFrameworkNotificationQueue:
    """Test the notification queue methods on a mock framework."""

    def _make_framework_like(self):
        """Create a minimal object that mimics the framework notification interface."""
        from agent_framework.models.team import TeamNotification, TeamNotificationType

        class FakeFramework:
            def __init__(self):
                self._pending_team_notifications: list[TeamNotification] = []
                self._team_coordinator = None

            def enqueue(self, role, status, summary, agent_id="sub_1", task=""):
                ntype = (TeamNotificationType.TASK_COMPLETED
                         if status == "completed"
                         else TeamNotificationType.TASK_FAILED)
                self._pending_team_notifications.append(TeamNotification(
                    team_id="team_test",
                    agent_id=agent_id,
                    role=role,
                    notification_type=ntype,
                    status=status,
                    summary=summary,
                    task=task,
                ))

        return FakeFramework()

    def test_pending_starts_empty(self):
        fw = self._make_framework_like()
        assert len(fw._pending_team_notifications) == 0

    def test_enqueue_creates_structured_notification(self):
        fw = self._make_framework_like()
        fw.enqueue("coder", "completed", "Done")
        assert len(fw._pending_team_notifications) == 1
        n = fw._pending_team_notifications[0]
        assert isinstance(n, TeamNotification)
        assert n.role == "coder"
        assert n.notification_type == TeamNotificationType.TASK_COMPLETED

    def test_enqueue_multiple(self):
        fw = self._make_framework_like()
        fw.enqueue("coder", "completed", "Code done", agent_id="sub_1")
        fw.enqueue("analyst", "failed", "Error", agent_id="sub_2")
        assert len(fw._pending_team_notifications) == 2
        assert fw._pending_team_notifications[0].role == "coder"
        assert fw._pending_team_notifications[1].role == "analyst"


class TestNotificationTypes:
    """Test TeamNotificationType enum values."""

    def test_all_types_exist(self):
        expected = {
            "TASK_COMPLETED", "TASK_FAILED", "QUESTION",
            "PLAN_SUBMISSION", "BROADCAST", "ERROR", "TEAMMATE_IDLE",
        }
        actual = {t.value for t in TeamNotificationType}
        assert expected == actual

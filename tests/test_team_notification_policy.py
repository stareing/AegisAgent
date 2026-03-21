"""Tests for TeamNotificationPolicy — event escalation rules.

Verifies:
1. Default policy escalates TASK_COMPLETED, TASK_FAILED, QUESTION, ERROR.
2. Default mail escalation covers ERROR_NOTICE, QUESTION.
3. Topic prefix matching for pub/sub mode.
4. Disabled policy blocks all escalation.
5. from_config creates valid policy.
"""

from __future__ import annotations

import pytest

from agent_framework.models.team import MailEventType, TeamNotificationType
from agent_framework.team.notification_policy import TeamNotificationPolicy


class TestDefaultPolicy:
    def test_escalates_task_completed(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_notification(TeamNotificationType.TASK_COMPLETED)

    def test_escalates_task_failed(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_notification(TeamNotificationType.TASK_FAILED)

    def test_escalates_question(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_notification(TeamNotificationType.QUESTION)

    def test_escalates_error(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_notification(TeamNotificationType.ERROR)

    def test_does_not_escalate_broadcast_by_default(self):
        policy = TeamNotificationPolicy()
        assert not policy.should_escalate_notification(TeamNotificationType.BROADCAST)


class TestMailEventEscalation:
    def test_progress_notice_does_not_escalate_by_default(self):
        policy = TeamNotificationPolicy()
        assert not policy.should_escalate_mail_event(MailEventType.PROGRESS_NOTICE)

    def test_error_notice_escalates(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_mail_event(MailEventType.ERROR_NOTICE)

    def test_question_escalates(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_mail_event(MailEventType.QUESTION)

    def test_broadcast_does_not_escalate_without_topic(self):
        policy = TeamNotificationPolicy()
        assert not policy.should_escalate_mail_event(MailEventType.BROADCAST_NOTICE)

    def test_status_ping_does_not_escalate(self):
        policy = TeamNotificationPolicy()
        assert not policy.should_escalate_mail_event(MailEventType.STATUS_PING)


class TestTopicPrefixEscalation:
    def test_findings_topic_escalates(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_mail_event(
            MailEventType.BROADCAST_NOTICE, topic="findings.security"
        )

    def test_alerts_topic_escalates(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_mail_event(
            MailEventType.BROADCAST_NOTICE, topic="alerts.critical"
        )

    def test_results_topic_escalates(self):
        policy = TeamNotificationPolicy()
        assert policy.should_escalate_mail_event(
            MailEventType.BROADCAST_NOTICE, topic="results.analysis"
        )

    def test_random_topic_does_not_escalate(self):
        policy = TeamNotificationPolicy()
        assert not policy.should_escalate_mail_event(
            MailEventType.BROADCAST_NOTICE, topic="chat.general"
        )


class TestDisabledPolicy:
    def test_disabled_blocks_notification(self):
        policy = TeamNotificationPolicy(enabled=False)
        assert not policy.should_escalate_notification(TeamNotificationType.TASK_COMPLETED)

    def test_disabled_blocks_mail_event(self):
        policy = TeamNotificationPolicy(enabled=False)
        assert not policy.should_escalate_mail_event(MailEventType.ERROR_NOTICE)

    def test_disabled_blocks_topic(self):
        policy = TeamNotificationPolicy(enabled=False)
        assert not policy.should_escalate_mail_event(
            MailEventType.BROADCAST_NOTICE, topic="findings.x"
        )


class TestFromConfig:
    def test_default_config(self):
        policy = TeamNotificationPolicy.from_config({})
        assert policy.enabled is True
        assert policy.batch_window_ms == 500
        assert policy.max_batch_size == 10

    def test_disabled_config(self):
        policy = TeamNotificationPolicy.from_config({"team_auto_notify_enabled": False})
        assert policy.enabled is False

    def test_custom_window(self):
        policy = TeamNotificationPolicy.from_config(
            {"team_auto_notify_batch_window_ms": 1000}
        )
        assert policy.batch_window_ms == 1000

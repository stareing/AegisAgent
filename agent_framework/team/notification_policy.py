"""TeamNotificationPolicy — determines which team events escalate to the main model.

Centralized policy for all 4 collaboration modes (Star/Mesh/Pub-Sub/Request-Reply).
Only events matching this policy will trigger a background notification turn.
"""

from __future__ import annotations

from typing import Any

from agent_framework.models.team import MailEventType, TeamNotificationType


# Default event types that always escalate to main model notification
_DEFAULT_ESCALATION_TYPES: frozenset[TeamNotificationType] = frozenset({
    TeamNotificationType.TASK_COMPLETED,
    TeamNotificationType.TASK_FAILED,
    TeamNotificationType.QUESTION,
    TeamNotificationType.ERROR,
    TeamNotificationType.TEAMMATE_IDLE,
})

# Default mail event types that escalate (for Mode B/C/D)
_DEFAULT_MAIL_ESCALATION_TYPES: frozenset[MailEventType] = frozenset({
    MailEventType.ERROR_NOTICE,
    MailEventType.QUESTION,
})

# Default topic prefixes that escalate (for Mode C pub/sub)
_DEFAULT_ESCALATION_TOPIC_PREFIXES: tuple[str, ...] = (
    "findings.",
    "alerts.",
    "errors.",
    "results.",
)


class TeamNotificationPolicy:
    """Policy for determining which team events escalate to the main model.

    Mode A (Star): PROGRESS_NOTICE(completed/failed), QUESTION → always escalate.
    Mode B (Mesh): Only escalate key BROADCAST_NOTICE or configured events.
    Mode C (Pub/Sub): Only escalate events matching configured topic prefixes.
    Mode D (Request/Reply): Escalate QUESTION/REPLY from lead-initiated conversations.
    """

    def __init__(
        self,
        enabled: bool = True,
        escalation_notification_types: frozenset[TeamNotificationType] | None = None,
        escalation_mail_types: frozenset[MailEventType] | None = None,
        escalation_topic_prefixes: tuple[str, ...] | None = None,
        batch_window_ms: int = 500,
        max_batch_size: int = 10,
    ) -> None:
        self.enabled = enabled
        self.escalation_notification_types = (
            escalation_notification_types or _DEFAULT_ESCALATION_TYPES
        )
        self.escalation_mail_types = (
            escalation_mail_types or _DEFAULT_MAIL_ESCALATION_TYPES
        )
        self.escalation_topic_prefixes = (
            escalation_topic_prefixes or _DEFAULT_ESCALATION_TOPIC_PREFIXES
        )
        self.batch_window_ms = batch_window_ms
        self.max_batch_size = max_batch_size

    def should_escalate_notification(
        self,
        notification_type: TeamNotificationType,
    ) -> bool:
        """Check if a TeamNotification should be escalated to main model."""
        if not self.enabled:
            return False
        return notification_type in self.escalation_notification_types

    def should_escalate_mail_event(
        self,
        event_type: MailEventType,
        topic: str = "",
    ) -> bool:
        """Check if a mail event should be escalated to main model.

        For pub/sub (Mode C), also checks topic prefix matching.
        """
        if not self.enabled:
            return False
        if event_type in self.escalation_mail_types:
            return True
        # Topic-based escalation for pub/sub mode
        if topic:
            return any(
                topic.startswith(prefix)
                for prefix in self.escalation_topic_prefixes
            )
        return False

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> TeamNotificationPolicy:
        """Create policy from config dict."""
        return cls(
            enabled=config.get("team_auto_notify_enabled", True),
            batch_window_ms=config.get("team_auto_notify_batch_window_ms", 500),
            max_batch_size=config.get("team_auto_notify_max_batch_size", 10),
        )

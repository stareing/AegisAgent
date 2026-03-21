"""RuntimeNotificationChannel — unified notification for background tasks + delegation events.

Extends the existing BackgroundNotifier pattern to also handle delegation event
notifications, so the coordinator can drain both in a single pass before each LLM call.

Architecture:
    BackgroundNotifier (bash tasks)    ──┐
                                         ├→ RuntimeNotificationChannel.drain_all()
    InteractionChannel (delegation)    ──┘
        → injects <runtime-notifications> into SessionState before LLM call

The existing BackgroundNotifier is preserved for backward compatibility.
This channel wraps it and adds delegation event draining.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from agent_framework.models.subagent import (AckLevel, DelegationEventSummary,
                                             DelegationEventType,
                                             RuntimeNotification,
                                             RuntimeNotificationType,
                                             SubAgentStatus)
from agent_framework.notification.background import (BackgroundNotification,
                                              BackgroundNotifier)

if TYPE_CHECKING:
    from agent_framework.subagent.interaction_channel import \
        InMemoryInteractionChannel


class RuntimeNotificationChannel:
    """Unified notification channel for background tasks and delegation events.

    Wraps BackgroundNotifier and InMemoryInteractionChannel into a single
    drain interface for the coordinator.
    """

    def __init__(
        self,
        bg_notifier: BackgroundNotifier | None = None,
        interaction_channel: InMemoryInteractionChannel | None = None,
    ) -> None:
        self._bg_notifier = bg_notifier or BackgroundNotifier()
        self._interaction_channel = interaction_channel
        # Track last-seen sequence per spawn_id for incremental draining
        self._last_seen_seq: dict[str, int] = {}
        # Track spawn_ids we're monitoring for delegation events
        self._monitored_spawns: set[str] = set()
        # AgentBus integration — optional, set via set_agent_bus()
        self._agent_bus: Any = None
        self._bus_drain_address: Any = None

    @property
    def bg_notifier(self) -> BackgroundNotifier:
        """Access the underlying BackgroundNotifier for backward compat."""
        return self._bg_notifier

    def set_interaction_channel(self, channel: InMemoryInteractionChannel) -> None:
        """Wire the interaction channel (may be set after construction)."""
        self._interaction_channel = channel

    def set_agent_bus(self, bus: Any, drain_address: Any) -> None:
        """Wire AgentBus as additional event source for drain_all().

        Args:
            bus: AgentBus instance.
            drain_address: BusAddress for the parent agent (used for drain).
        """
        self._agent_bus = bus
        self._bus_drain_address = drain_address

    def monitor_spawn(self, spawn_id: str) -> None:
        """Register a spawn_id for delegation event monitoring."""
        self._monitored_spawns.add(spawn_id)
        self._last_seen_seq.setdefault(spawn_id, 0)

    def unmonitor_spawn(self, spawn_id: str) -> None:
        """Stop monitoring a spawn_id (after completion/cleanup)."""
        self._monitored_spawns.discard(spawn_id)
        self._last_seen_seq.pop(spawn_id, None)

    def drain_all(self) -> list[RuntimeNotification]:
        """Drain all pending notifications — background tasks + delegation events.

        Non-blocking. Returns empty list if nothing new.
        """
        notifications: list[RuntimeNotification] = []

        # 1. Drain background tasks
        bg_results = self._bg_notifier.drain()
        for bg in bg_results:
            notifications.append(RuntimeNotification(
                notification_id=f"bg_{bg.task_id}",
                notification_type=RuntimeNotificationType.BACKGROUND_TASK,
                payload={
                    "task_id": bg.task_id,
                    "command": bg.command,
                    "output": bg.output,
                    "exit_code": bg.exit_code,
                    "timed_out": bg.timed_out,
                },
            ))

        # 2. Drain delegation events + advance ack_level to RECEIVED (§4)
        if self._interaction_channel is not None:
            for spawn_id in list(self._monitored_spawns):
                last_seq = self._last_seen_seq.get(spawn_id, 0)
                new_events = self._interaction_channel.drain_new_events(
                    spawn_id, last_seq
                )
                for event in new_events:
                    notifications.append(RuntimeNotification(
                        notification_id=f"del_{event.event_id}",
                        notification_type=RuntimeNotificationType.DELEGATION_EVENT,
                        payload={
                            "event_id": event.event_id,
                            "spawn_id": event.spawn_id,
                            "event_type": event.event_type.value,
                            "sequence_no": event.sequence_no,
                            "data": event.payload,
                            "requires_ack": event.requires_ack,
                        },
                    ))
                    self._last_seen_seq[spawn_id] = event.sequence_no
                    # Advance ack to RECEIVED on drain (boundary §4)
                    self._interaction_channel.ack_event(
                        spawn_id, event.event_id, AckLevel.RECEIVED
                    )

        # 3. Drain AgentBus team events
        if self._agent_bus is not None and self._bus_drain_address is not None:
            bus_events = self._agent_bus.drain(self._bus_drain_address, "team.**")
            for env in bus_events:
                notifications.append(RuntimeNotification(
                    notification_id=f"bus_{env.envelope_id}",
                    notification_type=RuntimeNotificationType.TEAM_EVENT,
                    payload={
                        "envelope_id": env.envelope_id,
                        "topic": env.topic,
                        "source_agent": env.source.agent_id,
                        "data": env.payload,
                    },
                ))

        return notifications

    @property
    def has_pending(self) -> bool:
        """Check if there are any pending notifications without draining."""
        if self._bg_notifier.has_pending:
            return True
        if self._interaction_channel is not None:
            for spawn_id in self._monitored_spawns:
                last_seq = self._last_seen_seq.get(spawn_id, 0)
                if self._interaction_channel.get_latest_sequence_no(spawn_id) > last_seq:
                    return True
        if self._agent_bus is not None and self._bus_drain_address is not None:
            if self._agent_bus.pending_count(self._bus_drain_address) > 0:
                return True
        return False

    def mark_projected(self, spawn_id: str, event_id: str) -> None:
        """Advance an event to PROJECTED ack level (boundary §4).

        Called by the coordinator after injecting the event summary
        into the parent's LLM context.
        """
        if self._interaction_channel is not None:
            self._interaction_channel.ack_event(
                spawn_id, event_id, AckLevel.PROJECTED
            )

    def mark_handled(self, spawn_id: str, event_id: str) -> None:
        """Advance an event to HANDLED ack level (boundary §4).

        Called after the parent has completed business processing for
        this event (e.g., answered an HITL request, consumed a checkpoint).
        """
        if self._interaction_channel is not None:
            self._interaction_channel.ack_event(
                spawn_id, event_id, AckLevel.HANDLED
            )

    def clear(self) -> None:
        """Full cleanup — only for shutdown."""
        self._bg_notifier.clear()
        self._monitored_spawns.clear()
        self._last_seen_seq.clear()

    @staticmethod
    def format_notifications(notifications: list[RuntimeNotification]) -> str:
        """Format notifications as XML block for context injection."""
        if not notifications:
            return ""

        lines = ["<runtime-notifications>"]
        for n in notifications:
            if n.notification_type == RuntimeNotificationType.BACKGROUND_TASK:
                p = n.payload
                status = "timed out" if p.get("timed_out") else (
                    "success" if p.get("exit_code", 0) == 0 else f"exit={p.get('exit_code')}"
                )
                output = str(p.get("output", ""))[:1000]
                lines.append(
                    f"  <background-task id=\"{html.escape(str(p.get('task_id', '')))}\" "
                    f"status=\"{status}\">{html.escape(output)}</background-task>"
                )
            elif n.notification_type == RuntimeNotificationType.DELEGATION_EVENT:
                p = n.payload
                lines.append(
                    f"  <delegation-event spawn=\"{html.escape(str(p.get('spawn_id', '')))}\" "
                    f"type=\"{html.escape(str(p.get('event_type', '')))}\" "
                    f"seq=\"{p.get('sequence_no', 0)}\">"
                    f"{html.escape(str(p.get('data', {})))}"
                    f"</delegation-event>"
                )
        lines.append("</runtime-notifications>")
        return "\n".join(lines)

    @staticmethod
    def summarize_delegation_events(
        notifications: list[RuntimeNotification],
    ) -> list[DelegationEventSummary]:
        """Extract DelegationEventSummary objects from delegation notifications.

        These summaries are suitable for injection into parent LLM context.
        """
        summaries: dict[str, DelegationEventSummary] = {}

        for n in notifications:
            if n.notification_type != RuntimeNotificationType.DELEGATION_EVENT:
                continue
            p = n.payload
            spawn_id = str(p.get("spawn_id", ""))
            event_type = str(p.get("event_type", ""))
            data = p.get("data", {})

            if spawn_id not in summaries:
                summaries[spawn_id] = DelegationEventSummary(
                    spawn_id=spawn_id,
                    status=SubAgentStatus.RUNNING,
                )

            summary = summaries[spawn_id]

            if event_type == DelegationEventType.PROGRESS.value:
                summary.summary = str(data.get("summary", summary.summary))
            elif event_type == DelegationEventType.QUESTION.value:
                summary.question = str(data.get("question", ""))
                summary.status = SubAgentStatus.WAITING_PARENT
            elif event_type == DelegationEventType.CONFIRMATION_REQUEST.value:
                summary.question = str(data.get("reason", ""))
                summary.status = SubAgentStatus.WAITING_USER
            elif event_type == DelegationEventType.CHECKPOINT.value:
                summary.checkpoint_notice = str(data.get("summary", ""))
            elif event_type == DelegationEventType.COMPLETED.value:
                summary.status = SubAgentStatus.COMPLETED
                summary.summary = str(data.get("summary", summary.summary))
            elif event_type == DelegationEventType.FAILED.value:
                summary.status = SubAgentStatus.FAILED
                summary.error_code = str(data.get("error_code", ""))
            elif event_type == DelegationEventType.CANCELLED.value:
                summary.status = SubAgentStatus.CANCELLED
            elif event_type == DelegationEventType.ARTIFACT_READY.value:
                artifact_name = str(data.get("name", ""))
                if artifact_name:
                    summary.artifacts_digest.append(artifact_name)

        return list(summaries.values())

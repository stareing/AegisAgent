"""Adapters bridging existing event pathways into AgentBus.

Each adapter converts one existing system's events into BusEnvelopes,
publishing them to AgentBus for unified consumption. Existing APIs
remain unchanged — adapters are additive wrappers.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.notification.envelope import BusAddress, BusEnvelope

if TYPE_CHECKING:
    from agent_framework.notification.bus import AgentBus

logger = get_logger(__name__)


class InteractionChannelAdapter:
    """Bridges DelegationEvent → BusEnvelope.

    Direction: InteractionChannel → AgentBus (one-way).
    Called by RuntimeNotificationChannel during drain to forward
    delegation events to the bus.
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus

    def forward_delegation_event(
        self, event_id: str, spawn_id: str, event_type: str,
        payload: dict, parent_run_id: str = "",
    ) -> None:
        """Forward a DelegationEvent as a BusEnvelope."""
        topic = f"agent.{spawn_id}.{event_type.lower()}"
        envelope = BusEnvelope(
            envelope_id=f"del_{event_id}",
            topic=topic,
            source=BusAddress(agent_id=spawn_id),
            target=BusAddress(agent_id="parent") if parent_run_id else None,
            payload={"_source": "interaction_channel", "event_type": event_type, **payload},
        )
        self._bus.publish(envelope)


class SiblingChannelAdapter:
    """Bridges SiblingMessage → BusEnvelope.

    Direction: SiblingChannel ↔ AgentBus (bidirectional potential,
    currently one-way: sibling → bus).
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus

    def forward_sibling_message(
        self, from_id: str, to_id: str, content: str,
        payload: dict | None = None, parent_run_id: str = "",
    ) -> None:
        """Forward a SiblingMessage as a BusEnvelope."""
        envelope = BusEnvelope(
            topic=f"agent.{to_id}.message",
            source=BusAddress(agent_id=from_id, group=parent_run_id),
            target=BusAddress(agent_id=to_id, group=parent_run_id),
            payload={"_source": "sibling_channel", "content": content, **(payload or {})},
        )
        self._bus.publish(envelope)


class BackgroundNotifierAdapter:
    """Bridges BackgroundNotification → BusEnvelope.

    Direction: BackgroundNotifier → AgentBus (one-way).
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus

    def forward_background_result(
        self, task_id: str, command: str, output: str,
        exit_code: int, timed_out: bool = False,
    ) -> None:
        """Forward a background task completion as a BusEnvelope."""
        envelope = BusEnvelope(
            topic="system.task.completed",
            source=BusAddress(agent_id="system"),
            payload={
                "_source": "background_notifier",
                "task_id": task_id,
                "command": command,
                "output": output[:500],
                "exit_code": exit_code,
                "timed_out": timed_out,
            },
        )
        self._bus.publish(envelope)


class EventBusAdapter:
    """Bridges EventBus signals → BusEnvelope.

    Direction: EventBus → AgentBus (one-way, observational).
    The EventBus "business logic must not depend" constraint is preserved.
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus

    def forward_event(self, event_name: str, payload: dict) -> None:
        """Forward an EventBus signal as a BusEnvelope."""
        envelope = BusEnvelope(
            topic=f"system.event.{event_name}",
            source=BusAddress(agent_id="system"),
            payload={"_source": "event_bus", "event_name": event_name, **payload},
        )
        self._bus.publish(envelope)

    def bind_to_event_bus(self, event_bus: Any) -> None:
        """Subscribe to all EventBus signals and forward them."""
        from agent_framework.infra.event_bus import EventEnvelope

        def _on_signal(sender: Any, envelope: EventEnvelope | None = None, **kwargs: Any) -> None:
            if envelope:
                self.forward_event(envelope.event_name, envelope.payload)

        # blinker: subscribe to the namespace's default signal
        try:
            ns = event_bus._namespace
            for signal_name in list(ns):
                ns.signal(signal_name).connect(_on_signal)
        except (AttributeError, TypeError):
            logger.debug("event_bus_adapter.bind_failed", hint="EventBus structure incompatible")

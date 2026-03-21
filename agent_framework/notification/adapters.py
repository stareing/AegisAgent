"""Adapters bridging existing event pathways into AgentBus.

Four adapters connect legacy channels to the unified bus without
modifying their original APIs. Each adapter is one-way or bidirectional.

Usage: call adapter.attach() after both the legacy channel and AgentBus
are initialized. Events flow automatically from that point.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.notification.envelope import BusAddress, BusEnvelope

if TYPE_CHECKING:
    from agent_framework.notification.bus import AgentBus

logger = get_logger(__name__)


class InteractionChannelAdapter:
    """Bridges DelegationEvent → BusEnvelope (one-way: child → bus).

    On attach, wraps the channel's emit_event to also publish to bus.
    Does NOT modify the channel's storage — events still live in
    InteractionChannel, bus gets a copy for unified routing.
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus
        self._original_emit: Any = None
        self._channel: Any = None

    def attach(self, channel: Any) -> None:
        """Monkey-patch channel.emit_event to forward to bus."""
        self._channel = channel
        self._original_emit = channel.emit_event

        def _patched_emit(
            spawn_id: str,
            parent_run_id: str,
            event_type: Any,
            payload: dict | None = None,
            requires_ack: bool = False,
        ) -> Any:
            result = self._original_emit(
                spawn_id, parent_run_id, event_type, payload, requires_ack,
            )
            # Forward to bus
            try:
                self._bus.publish(BusEnvelope(
                    topic=f"agent.{spawn_id}.{event_type.value.lower()}",
                    source=BusAddress(agent_id=spawn_id),
                    payload={
                        "_delegation_event_type": event_type.value,
                        "_parent_run_id": parent_run_id,
                        **(payload or {}),
                    },
                ))
            except Exception:
                pass  # Bus failure must not break delegation
            return result

        channel.emit_event = _patched_emit

    def detach(self) -> None:
        """Restore original emit_event."""
        if self._channel and self._original_emit:
            self._channel.emit_event = self._original_emit


class SiblingChannelAdapter:
    """Bridges SiblingMessage ↔ BusEnvelope (bidirectional).

    On attach, wraps channel.send to also publish to bus.
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus
        self._original_send: Any = None
        self._channel: Any = None

    def attach(self, channel: Any) -> None:
        self._channel = channel
        self._original_send = channel.send

        def _patched_send(
            from_spawn_id: str,
            to_spawn_id: str,
            parent_run_id: str,
            content: str,
            payload: dict | None = None,
        ) -> Any:
            result = self._original_send(
                from_spawn_id, to_spawn_id, parent_run_id, content, payload,
            )
            try:
                self._bus.publish(BusEnvelope(
                    topic=f"agent.{to_spawn_id}.message",
                    source=BusAddress(agent_id=from_spawn_id, group=parent_run_id),
                    target=BusAddress(agent_id=to_spawn_id, group=parent_run_id),
                    payload={"content": content, **(payload or {})},
                ))
            except Exception:
                pass
            return result

        channel.send = _patched_send

    def detach(self) -> None:
        if self._channel and self._original_send:
            self._channel.send = self._original_send


class BackgroundNotifierAdapter:
    """Bridges BackgroundNotification → BusEnvelope (one-way: system → bus).

    Wraps notifier.drain to also publish completed tasks to bus.
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus
        self._original_drain: Any = None
        self._notifier: Any = None

    def attach(self, notifier: Any) -> None:
        self._notifier = notifier
        self._original_drain = notifier.drain

        def _patched_drain() -> list:
            results = self._original_drain()
            for notification in results:
                try:
                    self._bus.publish(BusEnvelope(
                        topic="system.task.completed",
                        source=BusAddress(agent_id="system"),
                        payload={
                            "task_id": notification.task_id,
                            "command": notification.command,
                            "output": notification.output[:500],
                            "exit_code": notification.exit_code,
                        },
                    ))
                except Exception:
                    pass
            return results

        notifier.drain = _patched_drain

    def detach(self) -> None:
        if self._notifier and self._original_drain:
            self._notifier.drain = self._original_drain


class EventBusAdapter:
    """Bridges EventBus signals → BusEnvelope (one-way: observational).

    Subscribes to all EventBus signals and forwards to AgentBus.
    NOTE: The "business logic must not depend on EventBus" constraint
    still holds — these are observational forwarding only.
    """

    def __init__(self, bus: AgentBus) -> None:
        self._bus = bus
        self._subscribed_events: list[tuple[Any, Any]] = []

    def attach(self, event_bus: Any) -> None:
        """Subscribe to EventBus and forward events to AgentBus."""
        if not hasattr(event_bus, "subscribe"):
            return

        def _forward_handler(event_name: str, payload: dict) -> None:
            try:
                self._bus.publish(BusEnvelope(
                    topic=f"system.event.{event_name}",
                    source=BusAddress(agent_id="system"),
                    payload=payload,
                ))
            except Exception:
                pass

        # Subscribe to a wildcard or known events
        try:
            event_bus.subscribe("*", _forward_handler)
            self._subscribed_events.append((event_bus, _forward_handler))
        except Exception:
            pass

    def detach(self) -> None:
        for event_bus, handler in self._subscribed_events:
            try:
                event_bus.unsubscribe("*", handler)
            except Exception:
                pass
        self._subscribed_events.clear()

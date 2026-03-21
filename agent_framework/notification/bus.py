"""AgentBus — unified control bus for Agent Team interaction.

Provides publish/subscribe/drain/ack over a persistence backend.
Transport layer only — no business logic interpretation.
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import AckLevel
from agent_framework.notification.envelope import BusAddress, BusEnvelope
from agent_framework.notification.persistence import (BusPersistence,
                                                       InMemoryBusPersistence)
from agent_framework.notification.subscriber import (BusHandler, Subscription,
                                                      SubscriptionFilter)
from agent_framework.notification.topics import topic_matches

logger = get_logger(__name__)


class AgentBus:
    """Unified control bus for Agent Team interaction.

    Responsibilities:
    - Unified addressing and routing via BusAddress
    - Topic-based publish/subscribe with wildcard matching
    - Unicast / multicast / broadcast
    - Persistent delivery guarantees (via BusPersistence)
    - Event correlation tracking (via correlation_id / reply_to)

    NOT responsible for:
    - Business logic interpretation (consumers validate payloads)
    - Agent state management (Registries own state)
    - Policy decisions (routing rules in topics.py)
    - Agent creation/destruction (SubAgentRuntime)
    """

    def __init__(self, persistence: BusPersistence | None = None) -> None:
        self._persistence = persistence or InMemoryBusPersistence()
        self._lock = threading.Lock()
        self._subscriptions: dict[str, Subscription] = {}
        self._participants: dict[str, BusAddress] = {}  # agent_id -> address

    # ── Publish ────────────────────────────────────────────────

    def publish(self, envelope: BusEnvelope) -> None:
        """Publish an envelope to the bus. Routes to matching subscribers."""
        self._persistence.store(envelope)

        # Dispatch to push-mode subscribers
        with self._lock:
            subs = list(self._subscriptions.values())

        for sub in subs:
            if not topic_matches(sub.topic_pattern, envelope.topic):
                continue
            if sub.filter and not sub.filter.matches(envelope):
                continue
            try:
                sub.handler(envelope)
            except Exception:
                pass  # Handler exceptions never propagate to publisher

        logger.debug(
            "bus.published",
            envelope_id=envelope.envelope_id,
            topic=envelope.topic,
            source=envelope.source.agent_id,
        )

    def broadcast(
        self,
        topic: str,
        payload: dict,
        source: BusAddress,
        group: str = "",
    ) -> BusEnvelope:
        """Broadcast a message. group limits recipients to same group."""
        envelope = BusEnvelope(
            topic=topic,
            source=source.model_copy(update={"group": group}) if group else source,
            target=None,
            payload=payload,
        )
        self.publish(envelope)
        return envelope

    def send(
        self,
        topic: str,
        payload: dict,
        source: BusAddress,
        target: BusAddress,
    ) -> BusEnvelope:
        """Point-to-point send. Only target can receive."""
        envelope = BusEnvelope(
            topic=topic,
            source=source,
            target=target,
            payload=payload,
        )
        self.publish(envelope)
        return envelope

    def reply(
        self,
        original: BusEnvelope,
        payload: dict,
        source: BusAddress,
    ) -> BusEnvelope:
        """Reply to an envelope. Sets correlation_id and reply_to automatically."""
        envelope = BusEnvelope(
            topic=f"agent.{original.source.agent_id}.message",
            source=source,
            target=original.source,
            payload=payload,
            correlation_id=original.correlation_id or original.envelope_id,
            reply_to=original.envelope_id,
        )
        self.publish(envelope)
        return envelope

    # ── Subscribe (push mode) ─────────────────────────────────

    def subscribe(
        self,
        topic_pattern: str,
        handler: BusHandler,
        sub_filter: SubscriptionFilter | None = None,
    ) -> str:
        """Subscribe to topics matching pattern. Returns subscription_id."""
        sub_id = f"sub_{uuid.uuid4().hex[:12]}"
        sub = Subscription(sub_id, topic_pattern, handler, sub_filter)
        with self._lock:
            self._subscriptions[sub_id] = sub
        logger.debug("bus.subscribed", subscription_id=sub_id, pattern=topic_pattern)
        return sub_id

    def unsubscribe(self, subscription_id: str) -> None:
        """Remove a subscription."""
        with self._lock:
            self._subscriptions.pop(subscription_id, None)

    # ── Drain (pull mode) ─────────────────────────────────────

    def drain(
        self,
        address: BusAddress,
        topic_pattern: str = "**",
    ) -> list[BusEnvelope]:
        """Pull all pending messages for address. Marks as delivered."""
        pending = self._persistence.load_pending(
            address.agent_id, group=address.group,
        )
        result = []
        for env in pending:
            if not topic_matches(topic_pattern, env.topic):
                continue
            result.append(env)
            self._persistence.mark_delivered(env.envelope_id)
        return result

    def peek(
        self,
        address: BusAddress,
        topic_pattern: str = "**",
    ) -> list[BusEnvelope]:
        """View pending messages without marking as delivered."""
        pending = self._persistence.load_pending(
            address.agent_id, group=address.group,
        )
        return [e for e in pending if topic_matches(topic_pattern, e.topic)]

    def ack(self, envelope_id: str, level: AckLevel) -> None:
        """Acknowledge a message. Level can only advance."""
        self._persistence.mark_acked(envelope_id, level)

    # ── Query ─────────────────────────────────────────────────

    def pending_count(self, address: BusAddress) -> int:
        """Count of pending messages for address."""
        return len(self._persistence.load_pending(
            address.agent_id, group=address.group,
        ))

    def list_participants(self, group: str = "") -> list[BusAddress]:
        """List registered participants. Filter by group if specified."""
        with self._lock:
            if group:
                return [a for a in self._participants.values() if a.group == group]
            return list(self._participants.values())

    def get_envelope(self, envelope_id: str) -> BusEnvelope | None:
        """Lookup envelope by ID."""
        return self._persistence.get_envelope(envelope_id)

    # ── Lifecycle ─────────────────────────────────────────────

    def register_participant(self, address: BusAddress) -> None:
        """Register a participant on the bus."""
        with self._lock:
            self._participants[address.agent_id] = address
        self.publish(BusEnvelope(
            topic="system.bus.participant_joined",
            source=address,
            payload={"agent_id": address.agent_id, "group": address.group, "role": address.role},
        ))

    def unregister_participant(self, address: BusAddress) -> None:
        """Unregister a participant."""
        with self._lock:
            self._participants.pop(address.agent_id, None)
        self.publish(BusEnvelope(
            topic="system.bus.participant_left",
            source=address,
            payload={"agent_id": address.agent_id},
        ))

    def clear_group(self, group: str) -> int:
        """Remove all messages and participants for a group."""
        with self._lock:
            to_remove = [
                aid for aid, addr in self._participants.items()
                if addr.group == group
            ]
            for aid in to_remove:
                self._participants.pop(aid, None)
        return self._persistence.cleanup_group(group)

    def shutdown(self) -> None:
        """Close the bus and release resources."""
        self._persistence.cleanup_expired()
        self._persistence.close()
        with self._lock:
            self._subscriptions.clear()
            self._participants.clear()

"""TeamMailbox — bridge between Team protocol (MailEvent) and AgentBus (BusEnvelope).

Converts typed MailEvents into BusEnvelopes for transport, and converts
BusEnvelopes back to MailEvents on read. Handles payload validation,
broadcast expansion, topic subscriptions, and request/reply correlation.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.notification.envelope import BusAddress, BusEnvelope
from agent_framework.notification.topics import topic_matches

if TYPE_CHECKING:
    from agent_framework.notification.bus import AgentBus
    from agent_framework.team.registry import TeamRegistry

logger = get_logger(__name__)


class TeamMailbox:
    """Team event delivery channel.

    Responsibilities:
    - MailEvent → BusEnvelope mapping (send)
    - BusEnvelope → MailEvent mapping (read)
    - Typed Payload validation on send
    - Broadcast expansion (* → N point-to-point)
    - Topic publish/subscribe
    - Request/reply correlation

    NOT responsible for:
    - Business logic (TeamCoordinator/TeammateRuntime)
    - State truth (3 Registries)
    - Transport persistence (AgentBus/BusPersistence)
    """

    def __init__(self, bus: AgentBus, team_registry: TeamRegistry) -> None:
        self._bus = bus
        self._registry = team_registry
        self._lock = threading.Lock()
        self._subscriptions: dict[str, list[str]] = {}  # {topic_pattern: [agent_id]}

    # ── Send ───────────────────────────────────────────────────

    def send(self, event: Any) -> Any:
        """Send a point-to-point MailEvent. Validates payload, maps to BusEnvelope."""
        self._validate_payload(event)
        envelope = self._mail_to_envelope(event)
        self._bus.publish(envelope)
        logger.info(
            "team.mailbox.sent",
            event_type=event.event_type.value,
            from_agent=event.from_agent,
            to_agent=event.to_agent,
        )
        return event.model_copy(update={"event_id": envelope.envelope_id})

    def broadcast(self, event: Any) -> list[Any]:
        """Broadcast: expand to N point-to-point events (one per team member).

        Excludes the sender. Each recipient gets an independent event_id.
        """
        members = self._registry.list_members()
        sent = []
        for member in members:
            if member.agent_id == event.from_agent:
                continue
            individual = event.model_copy(update={
                "to_agent": member.agent_id,
                "event_id": f"evt_{uuid.uuid4().hex[:12]}",
            })
            sent.append(self.send(individual))
        return sent

    def reply(
        self, original_event_id: str, payload: dict, source: str,
        event_type: str | None = None,
    ) -> Any:
        """Reply to a MailEvent. Sets correlation_id and routes to original sender.

        event_type defaults based on the original event:
        - QUESTION → ANSWER
        - PLAN_SUBMISSION → APPROVAL_RESPONSE
        - SHUTDOWN_REQUEST → SHUTDOWN_ACK
        - TASK_HANDOFF_REQUEST → TASK_HANDOFF_RESPONSE
        - anything else → uses explicit event_type or ANSWER fallback

        Auto-injects request_id from original if not in reply payload.
        """
        original_env = self._bus.get_envelope(original_event_id)
        if original_env is None:
            raise ValueError(f"Original event {original_event_id} not found")

        # Auto-inject request_id from original if not in reply payload
        if "request_id" not in payload:
            orig_req_id = original_env.payload.get("_request_id") or original_env.payload.get("request_id") or original_event_id
            payload = {"request_id": orig_req_id, **payload}

        from agent_framework.models.team import MailEvent, MailEventType

        # Determine reply event type from original if not explicitly set
        orig_type_str = original_env.payload.get("_mail_event_type", "")
        _REPLY_MAP = {
            "QUESTION": MailEventType.ANSWER,
            "PLAN_SUBMISSION": MailEventType.APPROVAL_RESPONSE,
            "SHUTDOWN_REQUEST": MailEventType.SHUTDOWN_ACK,
            "TASK_HANDOFF_REQUEST": MailEventType.TASK_HANDOFF_RESPONSE,
            "TASK_CLAIM_REQUEST": MailEventType.TASK_CLAIMED_NOTICE,
            "STATUS_PING": MailEventType.STATUS_REPLY,
        }
        if event_type:
            reply_type = MailEventType(event_type)
        else:
            reply_type = _REPLY_MAP.get(orig_type_str, MailEventType.ANSWER)

        reply_event = MailEvent(
            team_id=original_env.source.group,
            from_agent=source,
            to_agent=original_env.source.agent_id,
            event_type=reply_type,
            correlation_id=original_event_id,
            payload=payload,
        )
        return self.send(reply_event)

    def publish(self, topic: str, payload: dict, source: str, team_id: str = "") -> list[Any]:
        """Publish to a topic. Expands to all subscribers."""
        with self._lock:
            targets: set[str] = set()
            for pattern, agent_ids in self._subscriptions.items():
                if topic_matches(pattern, topic):
                    targets.update(agent_ids)

        targets.discard(source)  # Don't send to self

        from agent_framework.models.team import MailEvent, MailEventType
        sent = []
        for target_id in targets:
            event = MailEvent(
                team_id=team_id,
                from_agent=source,
                to_agent=target_id,
                event_type=MailEventType.BROADCAST_NOTICE,
                payload={"_topic": topic, **payload},
            )
            sent.append(self.send(event))
        return sent

    # ── Subscribe ─────────────────────────────────────────────

    def subscribe(self, agent_id: str, topic_pattern: str) -> None:
        """Register a topic subscription for an agent."""
        with self._lock:
            if topic_pattern not in self._subscriptions:
                self._subscriptions[topic_pattern] = []
            if agent_id not in self._subscriptions[topic_pattern]:
                self._subscriptions[topic_pattern].append(agent_id)

    def unsubscribe(self, agent_id: str, topic_pattern: str) -> None:
        """Remove a topic subscription."""
        with self._lock:
            if topic_pattern in self._subscriptions:
                subs = self._subscriptions[topic_pattern]
                if agent_id in subs:
                    subs.remove(agent_id)

    # ── Receive ───────────────────────────────────────────────

    def read_inbox(
        self, agent_id: str, limit: int | None = None,
    ) -> list[Any]:
        """Read pending messages for an agent. Only marks returned messages as delivered.

        When limit is set, only drains up to limit messages — the rest
        stay pending for the next read. No messages are silently lost.
        """
        address = BusAddress(
            agent_id=agent_id,
            group=self._registry.get_team_id(),
        )
        if limit:
            envelopes = self._bus.drain_n(address, limit)
        else:
            envelopes = self._bus.drain(address)
        events = [self._envelope_to_mail(env) for env in envelopes]
        return events

    def read_unacked(self, agent_id: str) -> list[Any]:
        """Read messages that need acknowledgment."""
        address = BusAddress(
            agent_id=agent_id,
            group=self._registry.get_team_id(),
        )
        envelopes = self._bus.peek(address)
        return [
            self._envelope_to_mail(env)
            for env in envelopes
            if env.requires_ack
        ]

    def ack(self, agent_id: str, event_id: str) -> None:
        """Acknowledge receipt of a message."""
        from agent_framework.models.subagent import AckLevel
        self._bus.ack(event_id, AckLevel.RECEIVED)

    def pending_count(self, agent_id: str) -> int:
        address = BusAddress(
            agent_id=agent_id,
            group=self._registry.get_team_id(),
        )
        return self._bus.pending_count(address)

    # ── Mapping ────────────────────────────────────────────────

    def _mail_to_envelope(self, event: Any) -> BusEnvelope:
        """Convert MailEvent → BusEnvelope for bus transport."""
        team_id = event.team_id or self._registry.get_team_id()
        topic = f"team.{team_id}.{event.event_type.value.lower()}"

        target = None
        if event.to_agent and event.to_agent != "*":
            target = BusAddress(agent_id=event.to_agent, group=team_id)

        return BusEnvelope(
            envelope_id=event.event_id or f"env_{uuid.uuid4().hex[:12]}",
            topic=topic,
            source=BusAddress(agent_id=event.from_agent, group=team_id),
            target=target,
            payload={
                "_mail_event_type": event.event_type.value,
                "_from_agent": event.from_agent,
                "_request_id": getattr(event, "request_id", None) or "",
                "_correlation_id": getattr(event, "correlation_id", None) or "",
                **event.payload,
            },
            correlation_id=getattr(event, "correlation_id", None) or "",
            requires_ack=event.requires_ack,
        )

    def _envelope_to_mail(self, envelope: BusEnvelope) -> Any:
        """Convert BusEnvelope → MailEvent for business layer consumption."""
        from agent_framework.models.team import MailEvent, MailEventType

        payload = dict(envelope.payload)
        mail_type_str = payload.pop("_mail_event_type", "BROADCAST_NOTICE")
        from_agent = payload.pop("_from_agent", envelope.source.agent_id)
        request_id = payload.pop("_request_id", None) or None
        correlation_id = payload.pop("_correlation_id", None) or None

        return MailEvent(
            event_id=envelope.envelope_id,
            team_id=envelope.source.group,
            from_agent=from_agent,
            to_agent=envelope.target.agent_id if envelope.target else "",
            event_type=MailEventType(mail_type_str),
            created_at=envelope.created_at,
            request_id=request_id,
            correlation_id=correlation_id,
            requires_ack=envelope.requires_ack,
            ack_level=envelope.ack_level,
            payload=payload,
        )

    def _validate_payload(self, event: Any) -> None:
        """Validate typed payload if a validator exists for this event type."""
        from agent_framework.models.team import PAYLOAD_VALIDATORS
        validator = PAYLOAD_VALIDATORS.get(event.event_type)
        if validator and event.payload:
            validator(**event.payload)  # Raises ValidationError if invalid

"""Subscriber protocol and subscription filter for AgentBus."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from agent_framework.notification.envelope import BusEnvelope

# Handler signature: (envelope) -> None
BusHandler = Callable[[BusEnvelope], Any]


class SubscriptionFilter(BaseModel):
    """Filter conditions for subscriptions. All conditions are AND-combined.

    Empty lists mean "no filter on this dimension".
    """

    source_agent_ids: list[str] = Field(default_factory=list)
    exclude_agent_ids: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    min_priority: int = 0
    max_priority: int = 9
    requires_ack_only: bool = False
    payload_contains: dict = Field(default_factory=dict)

    def matches(self, envelope: BusEnvelope) -> bool:
        """Check if an envelope passes all filter conditions."""
        if self.source_agent_ids and envelope.source.agent_id not in self.source_agent_ids:
            return False
        if self.exclude_agent_ids and envelope.source.agent_id in self.exclude_agent_ids:
            return False
        if self.groups and envelope.source.group not in self.groups:
            return False
        if not (self.min_priority <= envelope.priority <= self.max_priority):
            return False
        if self.requires_ack_only and not envelope.requires_ack:
            return False
        if self.payload_contains:
            for key, val in self.payload_contains.items():
                if envelope.payload.get(key) != val:
                    return False
        return True


class Subscription:
    """Internal subscription record."""

    __slots__ = ("subscription_id", "topic_pattern", "handler", "filter")

    def __init__(
        self,
        subscription_id: str,
        topic_pattern: str,
        handler: BusHandler,
        sub_filter: SubscriptionFilter | None = None,
    ) -> None:
        self.subscription_id = subscription_id
        self.topic_pattern = topic_pattern
        self.handler = handler
        self.filter = sub_filter

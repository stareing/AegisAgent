"""BusEnvelope + BusAddress — unified message model for AgentBus.

All messages on the bus are wrapped in a BusEnvelope. The envelope is
immutable (frozen) to prevent consumers from tampering with shared messages.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from agent_framework.models.subagent import AckLevel


class BusAddress(BaseModel, frozen=True):
    """Participant address on the bus.

    agent_id is the only required field. group enables multicast
    (all members of a group receive the message).
    """

    agent_id: str
    run_id: str = ""
    role: str = ""
    group: str = ""


class BusEnvelope(BaseModel, frozen=True):
    """Immutable message envelope for the AgentBus.

    Design constraints:
    - frozen: consumers cannot mutate shared messages
    - payload: no schema validation at transport layer (business layer validates)
    - correlation_id: set manually by caller (bus does not infer causality)
    """

    envelope_id: str = Field(default_factory=lambda: f"env_{uuid.uuid4().hex[:12]}")
    topic: str = ""
    source: BusAddress = Field(default_factory=lambda: BusAddress(agent_id="system"))
    target: BusAddress | None = None
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""
    reply_to: str = ""
    ttl_ms: int = 0
    priority: int = 5
    requires_ack: bool = False
    ack_level: AckLevel = AckLevel.NONE

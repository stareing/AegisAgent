"""Sibling communication channel — allows sub-agents under the same parent to exchange messages.

Design: message-box model per spawn_id, parent_run_id scoped.
Sub-agents can send messages to siblings by spawn_id without going
through the parent agent. This enables direct coordination for
collaborative tasks (e.g., "Agent A writes code, Agent B reviews it").

Thread-safety: uses threading.Lock (same as InteractionChannel).
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class SiblingMessage(BaseModel):
    """A message between sibling sub-agents."""

    message_id: str = ""
    from_spawn_id: str = ""
    to_spawn_id: str = ""
    parent_run_id: str = ""
    content: str = ""
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    read: bool = False


class SiblingChannel:
    """In-memory message-box for sibling sub-agent communication.

    Scoped by parent_run_id — siblings can only communicate within the
    same parent run. Cross-run messaging is not allowed.
    """

    def __init__(self, max_messages_per_pair: int = 100) -> None:
        self._lock = threading.Lock()
        # Key: (parent_run_id, to_spawn_id) → list of messages
        self._mailboxes: dict[tuple[str, str], list[SiblingMessage]] = {}
        self._max_per_pair = max_messages_per_pair

    def send(
        self,
        from_spawn_id: str,
        to_spawn_id: str,
        parent_run_id: str,
        content: str,
        payload: dict | None = None,
    ) -> SiblingMessage:
        """Send a message to a sibling. Returns the sent message with assigned ID."""
        msg = SiblingMessage(
            message_id=f"sib_{uuid.uuid4().hex[:12]}",
            from_spawn_id=from_spawn_id,
            to_spawn_id=to_spawn_id,
            parent_run_id=parent_run_id,
            content=content,
            payload=payload or {},
        )
        key = (parent_run_id, to_spawn_id)
        with self._lock:
            if key not in self._mailboxes:
                self._mailboxes[key] = []
            box = self._mailboxes[key]
            if len(box) >= self._max_per_pair:
                logger.warning(
                    "sibling_channel.mailbox_full",
                    from_spawn_id=from_spawn_id,
                    to_spawn_id=to_spawn_id,
                    max=self._max_per_pair,
                )
                # Drop oldest to make room
                box.pop(0)
            box.append(msg)

        logger.info(
            "sibling_channel.sent",
            message_id=msg.message_id,
            from_spawn_id=from_spawn_id,
            to_spawn_id=to_spawn_id,
        )
        return msg

    def receive(
        self,
        spawn_id: str,
        parent_run_id: str,
        mark_read: bool = True,
    ) -> list[SiblingMessage]:
        """Receive all unread messages for a spawn_id. Marks them as read."""
        key = (parent_run_id, spawn_id)
        with self._lock:
            box = self._mailboxes.get(key, [])
            unread = [m for m in box if not m.read]
            if mark_read:
                for m in unread:
                    m.read = True
        return unread

    def peek(
        self,
        spawn_id: str,
        parent_run_id: str,
    ) -> list[SiblingMessage]:
        """Peek at all messages (read and unread) without marking as read."""
        key = (parent_run_id, spawn_id)
        with self._lock:
            return list(self._mailboxes.get(key, []))

    def list_siblings(self, parent_run_id: str) -> list[str]:
        """List all spawn_ids that have mailboxes under a parent run."""
        with self._lock:
            return list({
                to_id for (rid, to_id) in self._mailboxes if rid == parent_run_id
            })

    def clear_run(self, parent_run_id: str) -> int:
        """Remove all mailboxes for a parent run. Called on run cleanup."""
        with self._lock:
            to_remove = [
                key for key in self._mailboxes if key[0] == parent_run_id
            ]
            for key in to_remove:
                del self._mailboxes[key]
            return len(to_remove)

    def unread_count(self, spawn_id: str, parent_run_id: str) -> int:
        """Count of unread messages for a spawn_id."""
        key = (parent_run_id, spawn_id)
        with self._lock:
            box = self._mailboxes.get(key, [])
            return sum(1 for m in box if not m.read)

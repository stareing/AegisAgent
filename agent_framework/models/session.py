from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field

from agent_framework.models.message import Message


class SessionState(BaseModel):
    """Unique holder of current run's message history."""

    session_id: str = ""
    run_id: str = ""
    messages: list[Message] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_updated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    def append_message(self, msg: Message) -> None:
        self.messages.append(msg)
        self.last_updated_at = datetime.now(timezone.utc)

    def get_messages(self) -> list[Message]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()
        self.last_updated_at = datetime.now(timezone.utc)

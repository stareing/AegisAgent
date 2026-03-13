from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agent_framework.models.message import Message


class ToolTransactionGroup(BaseModel):
    """An atomic group of messages that cannot be split during context trimming.

    Examples:
    - An assistant message with tool_calls + all corresponding tool result messages
    - A spawn_agent request + its return result
    """

    group_id: str = ""
    group_type: Literal["TOOL_BATCH", "SUBAGENT_BATCH", "PLAIN_MESSAGES"] = (
        "PLAIN_MESSAGES"
    )
    messages: list[Message] = Field(default_factory=list)
    token_estimate: int = 0
    protected: bool = False

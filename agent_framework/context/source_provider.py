from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.memory import MemoryRecord
from agent_framework.models.message import Message

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentConfig, Skill
    from agent_framework.models.session import SessionState


class ContextSourceProvider:
    """Collects and formats context materials from various sources.

    This is where Saved Memory formatting happens (section 12.4).
    The memory layer only returns MemoryRecord lists.

    Format stability contract:
    - Same input MUST produce identical output (deterministic).
    - Memory display order: pinned first, then by kind, then alphabetical title.
    - Same memory kind uses a fixed template — no per-call variation.
    - Saved Memories block always uses role="system" in slot 3.
    - No randomness, no time-dependent formatting, no caller-dependent branching.
    """

    def collect_system_core(
        self, agent_config: AgentConfig, runtime_info: dict | None = None
    ) -> str:
        """Build the core system prompt."""
        parts = [agent_config.system_prompt]
        if runtime_info:
            info_lines = [f"- {k}: {v}" for k, v in runtime_info.items()]
            parts.append("\n## Runtime Info\n" + "\n".join(info_lines))
        return "\n\n".join(parts)

    def collect_skill_addon(self, active_skill: Skill | None) -> str | None:
        """Get skill-specific system prompt addon."""
        if active_skill and active_skill.system_prompt_addon:
            return active_skill.system_prompt_addon
        return None

    def collect_saved_memory_block(
        self, records: list[MemoryRecord]
    ) -> str | None:
        """Format saved memories into a text block for injection.

        This is where memory formatting happens - memory layer never does this.

        Deterministic ordering: pinned first, then by kind, then alphabetical title.
        Same input always produces identical output.
        """
        if not records:
            return None

        # Stable sort: pinned first → kind → title (alphabetical)
        sorted_records = sorted(
            records,
            key=lambda r: (not r.is_pinned, r.kind.value, r.title.lower()),
        )

        lines = ["## Saved Memories", ""]
        for r in sorted_records:
            prefix = "[pinned] " if r.is_pinned else ""
            lines.append(f"- {prefix}**{r.title}**: {r.content}")
            if r.tags:
                lines.append(f"  (tags: {', '.join(sorted(r.tags))})")
        return "\n".join(lines)

    def collect_session_groups(
        self, session_state: SessionState
    ) -> list[ToolTransactionGroup]:
        """Organize session messages into transaction groups."""
        messages = session_state.get_messages()
        groups: list[ToolTransactionGroup] = []

        i = 0
        while i < len(messages):
            msg = messages[i]

            if msg.role == "assistant" and msg.tool_calls:
                # Start of a tool transaction group
                group_msgs = [msg]
                expected_ids = {tc.id for tc in msg.tool_calls}
                j = i + 1
                while j < len(messages) and messages[j].role == "tool":
                    group_msgs.append(messages[j])
                    if messages[j].tool_call_id:
                        expected_ids.discard(messages[j].tool_call_id)
                    j += 1

                group_type = "TOOL_BATCH"
                # Check if any tool call is a spawn
                for tc in msg.tool_calls:
                    if "spawn" in tc.function_name:
                        group_type = "SUBAGENT_BATCH"
                        break

                groups.append(
                    ToolTransactionGroup(
                        group_id=str(uuid.uuid4()),
                        group_type=group_type,
                        messages=group_msgs,
                    )
                )
                i = j
            else:
                groups.append(
                    ToolTransactionGroup(
                        group_id=str(uuid.uuid4()),
                        group_type="PLAIN_MESSAGES",
                        messages=[msg],
                    )
                )
                i += 1

        return groups

    def collect_current_input(self, task_or_prompt: str) -> Message:
        """Wrap current user input as a Message."""
        return Message(role="user", content=task_or_prompt)

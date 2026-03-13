from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import Message

if TYPE_CHECKING:
    pass


class ContextBuilder:
    """Assembles context from slots and trims to fit token budget.

    Slot order (section 12.2):
    1. System Core
    2. Skill Addon
    3. Saved Memories
    4. Session History
    5. Current Input
    """

    def __init__(
        self,
        token_counter: Callable[[list[Message]], int] | None = None,
        max_context_tokens: int = 8192,
        reserve_for_output: int = 1024,
    ) -> None:
        self._max_tokens: int = max_context_tokens
        self._reserve_for_output: int = reserve_for_output
        self._token_counter = token_counter or self._rough_count

    def set_token_budget(self, max_tokens: int, reserve_for_output: int) -> None:
        self._max_tokens = max_tokens
        self._reserve_for_output = reserve_for_output

    def build_context(
        self,
        system_core: str,
        skill_addon: str | None,
        memory_block: str | None,
        session_groups: list[ToolTransactionGroup],
        current_input: Message,
    ) -> list[Message]:
        """Build the final message list for LLM consumption."""
        budget = self._max_tokens - self._reserve_for_output

        # Build system message
        system_parts = [system_core]
        if skill_addon:
            system_parts.append(skill_addon)
        if memory_block:
            system_parts.append(memory_block)
        system_text = "\n\n".join(system_parts)
        system_msg = Message(role="system", content=system_text)

        # Calculate remaining budget after system + current input
        fixed_messages = [system_msg, current_input]
        fixed_tokens = self.calculate_tokens(fixed_messages)
        remaining = budget - fixed_tokens

        if remaining <= 0:
            return [system_msg, current_input]

        # Trim session groups to fit remaining budget
        trimmed_groups = self._trim_session_groups(session_groups, remaining)

        # Assemble final context
        result = [system_msg]
        for group in trimmed_groups:
            result.extend(group.messages)
        result.append(current_input)

        return result

    def _trim_session_groups(
        self,
        groups: list[ToolTransactionGroup],
        token_limit: int,
    ) -> list[ToolTransactionGroup]:
        """Trim session groups from the oldest, preserving transaction atomicity."""
        if not groups:
            return []

        # Estimate tokens for each group
        for g in groups:
            if g.token_estimate == 0:
                g.token_estimate = self.calculate_tokens(g.messages)

        total = sum(g.token_estimate for g in groups)
        if total <= token_limit:
            return groups

        # Remove from the front (oldest) until we fit
        result = list(groups)
        while result and sum(g.token_estimate for g in result) > token_limit:
            if result[0].protected:
                break
            result.pop(0)

        return result

    def calculate_tokens(self, messages: list[Message]) -> int:
        return self._token_counter(messages)

    @staticmethod
    def _rough_count(messages: list[Message]) -> int:
        """Rough token estimate: ~4 chars per token."""
        total = 0
        for m in messages:
            if m.content:
                total += len(m.content) // 4
            if m.tool_calls:
                for tc in m.tool_calls:
                    total += len(str(tc.arguments)) // 4
        return max(total, 1)

    def build_spawn_seed(
        self,
        session_messages: list[Message],
        query: str,
        token_budget: int,
    ) -> list[Message]:
        """Build a context seed for a sub-agent from parent's session (doc 8.8).

        Selects the most relevant recent messages from the parent conversation
        and appends the sub-agent's task query, all within the token budget.
        """
        query_msg = Message(role="user", content=query)
        query_tokens = self.calculate_tokens([query_msg])
        remaining = token_budget - query_tokens

        if remaining <= 0:
            return [query_msg]

        # Take the most recent messages that fit within budget
        seed: list[Message] = []
        for msg in reversed(session_messages):
            msg_tokens = self.calculate_tokens([msg])
            if remaining - msg_tokens < 0:
                break
            seed.insert(0, msg)
            remaining -= msg_tokens

        seed.append(query_msg)
        return seed

    def _allocate_slot_budgets(self) -> dict[str, int]:
        """Allocate token budgets to each slot."""
        budget = self._max_tokens - self._reserve_for_output
        return {
            "system_core": int(budget * 0.15),
            "skill_addon": int(budget * 0.05),
            "saved_memories": int(budget * 0.10),
            "session_history": int(budget * 0.60),
            "current_input": int(budget * 0.10),
        }

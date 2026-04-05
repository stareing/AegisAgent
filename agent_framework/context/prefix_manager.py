"""PromptPrefixManager — frozen prefix cache for provider-side KV reuse (§14.8).

The frozen prefix contains the system identity + runtime environment +
optionally skill addon. It is reused across iterations within a run as
long as the inputs that compose it don't change.

Rotation triggers (prefix invalidated):
- system_core text changes (agent identity / prompt version)
- active skill changes (if skill_addon is frozen into prefix)
- runtime_info changes (OS, cwd — rare within a single run)

NOT rotation triggers (suffix-only changes):
- saved memories change
- session history changes
- current user input changes
- compression occurs
"""

from __future__ import annotations

import hashlib
from typing import Callable

from agent_framework.models.context import FrozenPromptPrefix
from agent_framework.models.message import Message


class PromptPrefixManager:
    """Manages frozen prompt prefix lifecycle within a run.

    Usage by ContextEngineer:
        prefix = manager.get_or_create(system_core, skill_addon, token_counter)
        # prefix.messages = [system_msg] (frozen, reusable)
        # Build suffix from memories + session + input
        # Final context = prefix.messages + suffix
    """

    def __init__(self) -> None:
        self._cached: FrozenPromptPrefix | None = None
        self._epoch: int = 0

    def get_or_create(
        self,
        system_core: str,
        skill_addon: str | None = None,
        token_counter: Callable[[list[Message]], int] | None = None,
    ) -> FrozenPromptPrefix:
        """Return cached prefix if inputs unchanged, else build a new one.

        Deterministic: same (system_core, skill_addon) → same prefix_hash.
        """
        current_hash = self._compute_hash(system_core, skill_addon)

        if self._cached and self._cached.prefix_hash == current_hash:
            return self._cached

        # Build new prefix
        system_parts = [system_core]
        if skill_addon:
            system_parts.append(skill_addon)
        system_text = "\n\n".join(system_parts)
        system_msg = Message(role="system", content=system_text)

        token_est = 0
        if token_counter:
            token_est = token_counter([system_msg])

        self._epoch += 1
        self._cached = FrozenPromptPrefix(
            prefix_epoch=self._epoch,
            messages=[system_msg],
            prefix_hash=current_hash,
            token_estimate=token_est,
            includes_skill_addon=skill_addon is not None,
        )
        return self._cached

    def should_rotate(
        self,
        system_core: str,
        skill_addon: str | None = None,
    ) -> bool:
        """Check if the prefix needs to be regenerated."""
        if self._cached is None:
            return True
        current_hash = self._compute_hash(system_core, skill_addon)
        return current_hash != self._cached.prefix_hash

    def invalidate(self) -> None:
        """Force prefix rotation on next get_or_create."""
        self._cached = None

    @property
    def current_prefix(self) -> FrozenPromptPrefix | None:
        return self._cached

    @staticmethod
    def _compute_hash(system_core: str, skill_addon: str | None) -> str:
        """Deterministic hash of prefix inputs."""
        content = system_core
        if skill_addon:
            content = f"{content}\n---SKILL---\n{skill_addon}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

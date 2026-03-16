"""HookRegistry — central registration and resolution of hooks.

Execution order within a hook point is deterministic:
1. priority (ascending)
2. plugin_id (lexicographic)
3. hook_id (lexicographic)
"""

from __future__ import annotations

from typing import Union

from agent_framework.infra.logger import get_logger
from agent_framework.hooks.errors import HookRegistrationError
from agent_framework.hooks.models import HookMeta, HookPoint
from agent_framework.hooks.protocol import AsyncHookProtocol, HookProtocol

logger = get_logger(__name__)

AnyHook = Union[HookProtocol, AsyncHookProtocol]


class HookRegistry:
    """Central hook registration and lookup.

    Thread-safety: designed for single-threaded setup, concurrent reads.
    All mutations (register/unregister) should happen during init phase.
    """

    def __init__(self) -> None:
        self._hooks: dict[str, AnyHook] = {}  # hook_id → hook

    def register(self, hook: AnyHook) -> None:
        """Register a hook. Raises on duplicate hook_id."""
        meta = hook.meta
        if meta.hook_id in self._hooks:
            raise HookRegistrationError(
                f"Duplicate hook_id: {meta.hook_id}",
                hook_id=meta.hook_id,
                plugin_id=meta.plugin_id,
            )
        self._hooks[meta.hook_id] = hook
        logger.info(
            "hook.registered",
            hook_id=meta.hook_id,
            plugin_id=meta.plugin_id,
            hook_point=meta.hook_point.value,
            priority=meta.priority,
        )

    def unregister(self, hook_id: str) -> None:
        """Remove a hook by ID. No-op if not found."""
        removed = self._hooks.pop(hook_id, None)
        if removed:
            logger.info("hook.unregistered", hook_id=hook_id)

    def list_hooks(
        self,
        hook_point: HookPoint | None = None,
        plugin_id: str | None = None,
        enabled_only: bool = True,
    ) -> list[HookMeta]:
        """List hook metadata, optionally filtered."""
        result: list[HookMeta] = []
        for hook in self._hooks.values():
            meta = hook.meta
            if enabled_only and not meta.enabled:
                continue
            if hook_point is not None and meta.hook_point != hook_point:
                continue
            if plugin_id is not None and meta.plugin_id != plugin_id:
                continue
            result.append(meta)
        return result

    def resolve_chain(self, hook_point: HookPoint) -> list[AnyHook]:
        """Return enabled hooks for a hook point in stable execution order.

        Order: priority (asc) → plugin_id (asc) → hook_id (asc)
        """
        candidates = [
            h for h in self._hooks.values()
            if h.meta.hook_point == hook_point and h.meta.enabled
        ]
        candidates.sort(
            key=lambda h: (h.meta.priority, h.meta.plugin_id, h.meta.hook_id)
        )
        return candidates

    def get_hook(self, hook_id: str) -> AnyHook | None:
        """Get a specific hook by ID."""
        return self._hooks.get(hook_id)

    def clear(self) -> None:
        """Remove all hooks. Used in tests and shutdown."""
        self._hooks.clear()

    @property
    def count(self) -> int:
        return len(self._hooks)

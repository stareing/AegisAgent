"""Module-level singleton for HookRegistry and HookExecutor.

Similar to get_tracing_manager() — lazily initialized, process-wide.
"""

from __future__ import annotations

from agent_framework.hooks.executor import HookExecutor
from agent_framework.hooks.registry import HookRegistry

_registry: HookRegistry | None = None
_executor: HookExecutor | None = None


def get_hook_registry() -> HookRegistry:
    """Return the process-wide HookRegistry singleton."""
    global _registry
    if _registry is None:
        _registry = HookRegistry()
    return _registry


def get_hook_executor() -> HookExecutor:
    """Return the process-wide HookExecutor singleton."""
    global _executor
    if _executor is None:
        _executor = HookExecutor(get_hook_registry())
    return _executor


def reset_hook_singletons() -> None:
    """Reset singletons — for tests only."""
    global _registry, _executor
    _registry = None
    _executor = None


class HookSubsystem:
    """Instance-level hook registry + executor bundle.

    Each AgentFramework instance should own one HookSubsystem.
    The global singletons remain for convenience but are discouraged
    when multiple framework instances coexist.
    """

    def __init__(self) -> None:
        self.registry = HookRegistry()
        self.executor = HookExecutor(self.registry)

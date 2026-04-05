"""Hook subsystem errors."""

from __future__ import annotations


class HookError(Exception):
    """Base error for hook subsystem."""

    def __init__(self, message: str, hook_id: str = "", plugin_id: str = "") -> None:
        super().__init__(message)
        self.hook_id = hook_id
        self.plugin_id = plugin_id


class HookTimeoutError(HookError):
    """Hook exceeded its timeout_ms."""


class HookDeniedError(HookError):
    """Hook returned DENY at a deniable hook point."""

    def __init__(self, message: str, hook_id: str = "", plugin_id: str = "") -> None:
        super().__init__(message, hook_id, plugin_id)


class HookRegistrationError(HookError):
    """Invalid hook registration (duplicate ID, invalid hook point, etc.)."""


class InvalidHookResultError(HookError):
    """Hook returned an invalid result (e.g. DENY at non-deniable point)."""

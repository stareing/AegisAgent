"""Hook protocols — interface contracts for hook implementations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agent_framework.hooks.models import HookContext, HookMeta, HookResult


@runtime_checkable
class HookProtocol(Protocol):
    """Synchronous hook interface."""

    @property
    def meta(self) -> HookMeta: ...

    def execute(self, context: HookContext) -> HookResult: ...


@runtime_checkable
class AsyncHookProtocol(Protocol):
    """Asynchronous hook interface."""

    @property
    def meta(self) -> HookMeta: ...

    async def execute(self, context: HookContext) -> HookResult: ...

"""HookDispatchService — single entry point for firing hooks.

Consolidates execute_chain + interpret_hook_results + async/sync bridging
into one service. All kernel components should call this instead of
manually constructing HookContext, running execute_chain, and interpreting.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.hooks.errors import HookDeniedError
from agent_framework.hooks.executor import HookExecutor
from agent_framework.hooks.interpreter import HookChainOutcome, interpret_hook_results
from agent_framework.models.hook import HookContext, HookPoint

logger = get_logger(__name__)

_BLOCKING_TIMEOUT_S = 5


class HookDispatchService:
    """Unified hook dispatch for the entire framework.

    Provides:
    - fire(): async entry point (use in async code)
    - fire_sync(): sync entry point (handles async bridging internally)

    Both construct HookContext, run the chain, interpret results, and
    handle HookDeniedError. Callers only need to check the returned
    HookChainOutcome.
    """

    def __init__(self, executor: HookExecutor) -> None:
        self._executor = executor

    # ------------------------------------------------------------------
    # Async entry
    # ------------------------------------------------------------------

    async def fire(
        self,
        hook_point: HookPoint,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        iteration_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HookChainOutcome:
        """Fire a hook chain asynchronously and return interpreted outcome.

        Raises HookDeniedError if a hook denies at a deniable point.
        """
        ctx = HookContext(
            run_id=run_id,
            agent_id=agent_id,
            user_id=user_id,
            iteration_id=iteration_id,
            payload=payload or {},
        )
        results = await self._executor.execute_chain(hook_point, ctx)
        return interpret_hook_results(hook_point, results)

    # ------------------------------------------------------------------
    # Sync entry (bridges async automatically)
    # ------------------------------------------------------------------

    def fire_sync(
        self,
        hook_point: HookPoint,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        iteration_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HookChainOutcome:
        """Fire a hook chain from synchronous code.

        Handles the async-to-sync bridge: if already inside an event loop,
        runs the coroutine in a background thread with its own loop.

        Raises HookDeniedError if a hook denies at a deniable point.
        """
        coro = self.fire(
            hook_point,
            run_id=run_id,
            agent_id=agent_id,
            user_id=user_id,
            iteration_id=iteration_id,
            payload=payload,
        )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result(timeout=_BLOCKING_TIMEOUT_S)
        else:
            return asyncio.run(coro)

    # ------------------------------------------------------------------
    # Convenience: fire-and-forget (swallows all errors including DENY)
    # ------------------------------------------------------------------

    async def fire_advisory(
        self,
        hook_point: HookPoint,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        user_id: str | None = None,
        iteration_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HookChainOutcome:
        """Fire a hook chain that is advisory-only (never raises, never blocks).

        Use for POST_* hooks where the result should never affect control flow.
        """
        try:
            return await self.fire(
                hook_point,
                run_id=run_id,
                agent_id=agent_id,
                user_id=user_id,
                iteration_id=iteration_id,
                payload=payload,
            )
        except Exception:
            return HookChainOutcome()

    def fire_sync_advisory(
        self,
        hook_point: HookPoint,
        *,
        run_id: str | None = None,
        agent_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> HookChainOutcome:
        """Sync fire-and-forget: swallows all errors."""
        try:
            return self.fire_sync(
                hook_point, run_id=run_id, agent_id=agent_id, payload=payload,
            )
        except Exception:
            return HookChainOutcome()

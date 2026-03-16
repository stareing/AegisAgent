"""HookExecutor — runs hook chains with timeout, failure policy, and validation.

Behavior rules:
- Sync hooks: serial execution, stable order
- Async hooks: may run concurrently for computation, but observable
  side effects still go through the unified commit chain
- DENY is only valid at DENIABLE_HOOK_POINTS
- Hook failures follow their failure_policy (ignore/warn/fail_closed)
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Union

from agent_framework.infra.logger import get_logger
from agent_framework.hooks.errors import (
    HookDeniedError,
    HookTimeoutError,
    InvalidHookResultError,
)
from agent_framework.hooks.models import (
    DENIABLE_HOOK_POINTS,
    HookContext,
    HookFailurePolicy,
    HookPoint,
    HookResult,
    HookResultAction,
)
from agent_framework.hooks.protocol import AsyncHookProtocol, HookProtocol
from agent_framework.hooks.registry import AnyHook, HookRegistry

logger = get_logger(__name__)


class HookExecutor:
    """Executes hook chains at framework-defined extension points.

    The executor is the ONLY entry point for running hooks. Framework
    components call execute_chain() — they never invoke hooks directly.
    """

    def __init__(self, registry: HookRegistry) -> None:
        self._registry = registry

    async def execute_chain(
        self,
        hook_point: HookPoint,
        context: HookContext,
    ) -> list[HookResult]:
        """Execute all enabled hooks for a hook point in order.

        Returns list of HookResults. If any hook returns DENY at a
        deniable hook point with fail_closed policy, raises HookDeniedError.

        For non-deniable points, DENY results are treated as invalid
        and converted to NOOP with a warning.
        """
        chain = self._registry.resolve_chain(hook_point)
        if not chain:
            return []

        results: list[HookResult] = []
        is_deniable = hook_point in DENIABLE_HOOK_POINTS

        for hook in chain:
            meta = hook.meta
            try:
                result = await self._execute_single(hook, context)
                result.hook_id = meta.hook_id

                # Validate DENY at non-deniable points
                if result.action == HookResultAction.DENY and not is_deniable:
                    logger.warning(
                        "hook.invalid_deny",
                        hook_id=meta.hook_id,
                        hook_point=hook_point.value,
                        message="DENY not allowed at this hook point, converting to NOOP",
                    )
                    result = HookResult(
                        hook_id=meta.hook_id,
                        action=HookResultAction.NOOP,
                        message=f"DENY invalid at {hook_point.value}, ignored",
                    )

                results.append(result)

                # Check if DENY should abort
                if result.action == HookResultAction.DENY and is_deniable:
                    logger.info(
                        "hook.denied",
                        hook_id=meta.hook_id,
                        hook_point=hook_point.value,
                        message=result.message,
                    )
                    raise HookDeniedError(
                        result.message or f"Denied by hook {meta.hook_id}",
                        hook_id=meta.hook_id,
                        plugin_id=meta.plugin_id,
                    )

            except HookDeniedError:
                raise  # Propagate deny
            except HookTimeoutError:
                result = self._handle_failure(
                    meta, f"Hook timed out after {meta.timeout_ms}ms"
                )
                results.append(result)
            except Exception as e:
                result = self._handle_failure(meta, str(e))
                results.append(result)

        return results

    async def _execute_single(
        self, hook: AnyHook, context: HookContext
    ) -> HookResult:
        """Execute a single hook with timeout enforcement."""
        meta = hook.meta
        timeout_s = meta.timeout_ms / 1000.0
        start = time.monotonic()

        try:
            is_async = inspect.iscoroutinefunction(hook.execute)
            if is_async:
                result = await asyncio.wait_for(
                    hook.execute(context), timeout=timeout_s
                )
            else:
                # Sync hook — run in thread pool to avoid blocking
                result = await asyncio.wait_for(
                    asyncio.to_thread(hook.execute, context),
                    timeout=timeout_s,
                )
        except asyncio.TimeoutError:
            raise HookTimeoutError(
                f"Hook {meta.hook_id} timed out after {meta.timeout_ms}ms",
                hook_id=meta.hook_id,
                plugin_id=meta.plugin_id,
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.debug(
            "hook.executed",
            hook_id=meta.hook_id,
            action=result.action.value,
            elapsed_ms=elapsed_ms,
        )
        return result

    def _handle_failure(self, meta: "HookMeta", error_msg: str) -> HookResult:
        """Apply failure policy and return appropriate result."""
        from agent_framework.hooks.models import HookMeta  # noqa: F811

        policy = meta.failure_policy

        if policy == HookFailurePolicy.IGNORE:
            return HookResult(
                hook_id=meta.hook_id,
                action=HookResultAction.NOOP,
            )

        if policy == HookFailurePolicy.WARN:
            logger.warning(
                "hook.failed",
                hook_id=meta.hook_id,
                plugin_id=meta.plugin_id,
                error=error_msg,
                failure_policy="warn",
            )
            return HookResult(
                hook_id=meta.hook_id,
                action=HookResultAction.NOOP,
                message=f"Hook failed: {error_msg}",
                error_code="HOOK_EXECUTION_FAILED",
            )

        if policy == HookFailurePolicy.FAIL_CLOSED:
            logger.error(
                "hook.failed_closed",
                hook_id=meta.hook_id,
                plugin_id=meta.plugin_id,
                error=error_msg,
            )
            raise RuntimeError(
                f"Hook {meta.hook_id} failed with fail_closed policy: {error_msg}"
            )

        # Default: warn
        logger.warning("hook.failed", hook_id=meta.hook_id, error=error_msg)
        return HookResult(
            hook_id=meta.hook_id,
            action=HookResultAction.NOOP,
            message=error_msg,
        )

"""DelegationHookPolicyApplier — shared PRE_DELEGATION hook logic.

Eliminates duplication between sync and async delegation paths.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_framework.hooks.dispatcher import HookDispatchService
from agent_framework.hooks.errors import HookDeniedError
from agent_framework.hooks.payloads import delegation_pre_payload
from agent_framework.models.hook import HookPoint

if TYPE_CHECKING:
    from agent_framework.models.subagent import SubAgentSpec


async def apply_pre_delegation_hooks(
    dispatcher: HookDispatchService,
    spec: SubAgentSpec,
    *,
    is_async: bool = False,
    confirmation_handler: Any = None,
) -> SubAgentSpec:
    """Run PRE_DELEGATION hooks and apply MODIFY/CONFIRMATION results to spec.

    Returns the (possibly modified) spec.
    Raises HookDeniedError if a hook denies.
    """
    outcome = await dispatcher.fire(
        HookPoint.PRE_DELEGATION,
        run_id=spec.parent_run_id,
        payload=delegation_pre_payload(
            task_input=spec.task_input,
            mode=str(spec.mode.value),
            memory_scope=str(spec.memory_scope.value),
            deadline_ms=spec.deadline_ms,
            is_async=is_async,
        ),
    )

    # Apply MODIFY
    updates: dict[str, Any] = {}
    if "task_input_override" in outcome.modifications:
        updates["task_input"] = outcome.modifications["task_input_override"]
    if "deadline_ms_override" in outcome.modifications:
        updates["deadline_ms"] = int(outcome.modifications["deadline_ms_override"])
    if updates:
        spec = spec.model_copy(update=updates)

    # REQUEST_CONFIRMATION
    if outcome.needs_confirmation:
        if confirmation_handler is not None:
            approved = await confirmation_handler.request_confirmation(
                "spawn_agent",
                {"task_input": spec.task_input[:200]},
                outcome.confirmation_reason,
            )
            if not approved:
                from agent_framework.models.subagent import SubAgentResult
                raise _DelegationConfirmationDenied(spec.spawn_id)

    return spec


class _DelegationConfirmationDenied(Exception):
    """Internal: user denied delegation confirmation."""
    def __init__(self, spawn_id: str) -> None:
        super().__init__("User denied delegation confirmation")
        self.spawn_id = spawn_id

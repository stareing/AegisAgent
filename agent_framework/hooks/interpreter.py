"""HookResultInterpreter — processes hook chain results into actionable decisions.

Bridges the gap between raw HookResult list and framework behavior:
- MODIFY: applies modified_payload fields (whitelisted per hook point)
- REQUEST_CONFIRMATION: signals that user confirmation is needed
- EMIT_ARTIFACT: collects emitted artifacts for later registration
- DENY: already handled by HookExecutor (raises HookDeniedError)
- ALLOW/NOOP: no action needed
"""

from __future__ import annotations

from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.hook import HookPoint, HookResult, HookResultAction

logger = get_logger(__name__)

# Per-hook-point whitelist of fields that MODIFY can change
_MODIFIABLE_FIELDS: dict[HookPoint, frozenset[str]] = {
    HookPoint.PRE_TOOL_USE: frozenset({
        "display_name", "sanitized_arguments", "idempotency_key", "user_visible_summary",
    }),
    HookPoint.CONTEXT_PRE_BUILD: frozenset({
        "extra_instructions", "compression_preference",
    }),
    HookPoint.PRE_DELEGATION: frozenset({
        "task_input_override", "deadline_ms_override",
    }),
    HookPoint.MEMORY_PRE_RECORD: frozenset({
        "content", "tags", "title",
    }),
}


class HookChainOutcome:
    """Processed outcome of a hook chain execution."""

    __slots__ = (
        "should_proceed", "needs_confirmation", "confirmation_reason",
        "modifications", "emitted_artifacts", "audit_records",
    )

    def __init__(self) -> None:
        self.should_proceed: bool = True
        self.needs_confirmation: bool = False
        self.confirmation_reason: str = ""
        self.modifications: dict[str, Any] = {}
        self.emitted_artifacts: list[dict[str, Any]] = []
        self.audit_records: list[dict[str, Any]] = []


def interpret_hook_results(
    hook_point: HookPoint,
    results: list[HookResult],
) -> HookChainOutcome:
    """Process a list of HookResults into an actionable outcome.

    Rules:
    - DENY: already raised as HookDeniedError by executor, won't appear here
    - MODIFY: only whitelisted fields for the hook point are applied
    - REQUEST_CONFIRMATION: sets needs_confirmation flag
    - EMIT_ARTIFACT: collects artifacts
    - ALLOW/NOOP: pass-through
    - Multiple MODIFYs: last writer wins per field
    - Multiple REQUEST_CONFIRMATIONs: first reason is used
    """
    outcome = HookChainOutcome()
    whitelist = _MODIFIABLE_FIELDS.get(hook_point, frozenset())

    for result in results:
        # Collect audit data from all hooks
        if result.audit_data:
            outcome.audit_records.append(result.audit_data)

        if result.action == HookResultAction.MODIFY:
            if result.modified_payload:
                for key, value in result.modified_payload.items():
                    if key in whitelist:
                        outcome.modifications[key] = value
                    else:
                        logger.warning(
                            "hook.modify_blocked",
                            hook_id=result.hook_id,
                            hook_point=hook_point.value,
                            field=key,
                            reason="field not in whitelist",
                        )

        elif result.action == HookResultAction.REQUEST_CONFIRMATION:
            if not outcome.needs_confirmation:
                outcome.needs_confirmation = True
                outcome.confirmation_reason = result.message or "Hook requested confirmation"

        elif result.action == HookResultAction.EMIT_ARTIFACT:
            outcome.emitted_artifacts.extend(result.emitted_artifacts)

    return outcome

"""AuditNotifyHook — post-run audit logging and notification.

Fires at RUN_FINISH and RUN_ERROR to produce structured audit records
that external systems can consume. Does not modify control flow.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from agent_framework.models.hook import (
    HookCategory,
    HookContext,
    HookExecutionMode,
    HookFailurePolicy,
    HookMeta,
    HookPoint,
    HookResult,
    HookResultAction,
)

# Type for external notification callback
NotifyCallback = Callable[[dict], None]


class AuditNotifyHook:
    """Generates audit summary at run finish/error.

    Optionally calls a notify_callback with the audit data for
    external integration (webhook, Slack, email, etc.).
    """

    def __init__(
        self,
        hook_point: HookPoint = HookPoint.RUN_FINISH,
        notify_callback: NotifyCallback | None = None,
        hook_id: str | None = None,
    ) -> None:
        self._notify_callback = notify_callback
        resolved_id = hook_id or f"builtin.audit_notify.{hook_point.value}"
        self._meta = HookMeta(
            hook_id=resolved_id,
            plugin_id="builtin",
            name="Audit Notify",
            hook_point=hook_point,
            category=HookCategory.COMMAND,
            description="Produces structured audit records at run completion or failure",
            execution_mode=HookExecutionMode.SYNC,
            failure_policy=HookFailurePolicy.IGNORE,
            priority=900,  # Run late — after business hooks
            timeout_ms=2000,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        audit_record = {
            "event": self._meta.hook_point.value,
            "run_id": context.run_id,
            "agent_id": context.agent_id,
            "user_id": context.user_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload_summary": {
                k: str(v)[:200] for k, v in context.payload.items()
            },
        }

        if self._notify_callback is not None:
            try:
                self._notify_callback(audit_record)
            except Exception:
                pass  # Best-effort notification

        return HookResult(
            action=HookResultAction.NOOP,
            audit_data=audit_record,
        )

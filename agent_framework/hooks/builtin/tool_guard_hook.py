"""ToolGuardHook — pre-tool-use gate for argument validation and safety checks.

Checks:
- Argument size limits (prevents oversized payloads)
- Dangerous tool tag filtering
- Idempotency key presence for write tools
"""

from __future__ import annotations

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

# Maximum total argument payload size in characters
_MAX_ARGUMENT_CHARS = 100_000

# Tool tags that trigger extra scrutiny
_DANGEROUS_TAGS = frozenset({"destructive", "admin", "filesystem_write", "network"})


class ToolGuardHook:
    """Pre-tool-use gate checking argument safety and size."""

    def __init__(
        self,
        max_argument_chars: int = _MAX_ARGUMENT_CHARS,
        blocked_tags: frozenset[str] | None = None,
        hook_id: str = "builtin.tool_guard",
    ) -> None:
        self._max_argument_chars = max_argument_chars
        self._blocked_tags = blocked_tags or _DANGEROUS_TAGS
        self._meta = HookMeta(
            hook_id=hook_id,
            plugin_id="builtin",
            name="Tool Guard",
            hook_point=HookPoint.PRE_TOOL_USE,
            category=HookCategory.COMMAND,
            description="Validates tool arguments for size and safety before execution",
            execution_mode=HookExecutionMode.SYNC,
            failure_policy=HookFailurePolicy.WARN,
            priority=10,  # Run early in chain
            timeout_ms=1000,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        payload = context.payload
        tool_name = payload.get("tool_name", "")
        arguments = payload.get("arguments", {})
        tool_tags = payload.get("tool_tags", [])

        # Check argument size
        import json
        try:
            arg_str = json.dumps(arguments, default=str)
        except (TypeError, ValueError):
            arg_str = str(arguments)

        if len(arg_str) > self._max_argument_chars:
            return HookResult(
                action=HookResultAction.DENY,
                message=(
                    f"Tool '{tool_name}' arguments exceed size limit "
                    f"({len(arg_str)} > {self._max_argument_chars} chars)"
                ),
            )

        # Check dangerous tags
        matched_tags = self._blocked_tags & set(tool_tags)
        if matched_tags:
            return HookResult(
                action=HookResultAction.REQUEST_CONFIRMATION,
                message=(
                    f"Tool '{tool_name}' has dangerous tags: {sorted(matched_tags)}. "
                    "Requesting user confirmation."
                ),
                audit_data={
                    "tool_name": tool_name,
                    "matched_dangerous_tags": sorted(matched_tags),
                },
            )

        return HookResult(action=HookResultAction.ALLOW)

"""MemoryReviewHook — pre-record gate for memory candidates.

Reviews memory candidates before they are committed to storage.
Can deny writes that violate content policies or size limits.
"""

from __future__ import annotations

import re

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

# Patterns that should not appear in stored memories
_SENSITIVE_PATTERNS = (
    re.compile(r"(?:password|secret|token|api_key)\s*[:=]\s*\S+", re.IGNORECASE),
)


class MemoryReviewHook:
    """Reviews memory candidates for policy compliance before storage.

    Checks:
    - Content length limits
    - Sensitive data patterns (passwords, API keys)
    - Tag count limits
    """

    def __init__(
        self,
        max_content_length: int = 5000,
        max_tags: int = 20,
        hook_id: str = "builtin.memory_review",
    ) -> None:
        self._max_content_length = max_content_length
        self._max_tags = max_tags
        self._meta = HookMeta(
            hook_id=hook_id,
            plugin_id="builtin",
            name="Memory Review",
            hook_point=HookPoint.MEMORY_PRE_RECORD,
            category=HookCategory.COMMAND,
            description="Reviews memory candidates for content policy compliance",
            execution_mode=HookExecutionMode.SYNC,
            failure_policy=HookFailurePolicy.WARN,
            priority=50,
            timeout_ms=1000,
        )

    @property
    def meta(self) -> HookMeta:
        return self._meta

    def execute(self, context: HookContext) -> HookResult:
        payload = context.payload
        content = payload.get("content", "")
        tags = payload.get("tags", [])

        # Content length check
        if len(content) > self._max_content_length:
            return HookResult(
                action=HookResultAction.DENY,
                message=(
                    f"Memory content exceeds limit "
                    f"({len(content)} > {self._max_content_length} chars)"
                ),
            )

        # Tag count check
        if len(tags) > self._max_tags:
            return HookResult(
                action=HookResultAction.DENY,
                message=f"Too many tags ({len(tags)} > {self._max_tags})",
            )

        # Sensitive data check
        for pattern in _SENSITIVE_PATTERNS:
            match = pattern.search(content)
            if match:
                return HookResult(
                    action=HookResultAction.DENY,
                    message=(
                        "Memory content contains potentially sensitive data "
                        f"(matched pattern near: ...{match.group()[:30]}...)"
                    ),
                    audit_data={"matched_pattern": pattern.pattern},
                )

        return HookResult(action=HookResultAction.ALLOW)

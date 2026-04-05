from __future__ import annotations

from agent_framework.models.agent import (
    ApprovalMode,
    CapabilityPolicy,
    PLAN_MODE_ALLOWED_CATEGORIES,
    PLAN_MODE_ALLOWED_TOOLS,
    PLAN_MODE_BLOCKED_TOOLS,
)
from agent_framework.models.tool import ToolEntry


def apply_capability_policy(
    tools: list[ToolEntry],
    policy: CapabilityPolicy,
    approval_mode: ApprovalMode | None = None,
) -> list[ToolEntry]:
    """Filter tools according to the capability policy.

    Priority (section 10.4):
    1. ApprovalMode PLAN — restricts to read-only tools
    2. CapabilityPolicy defines the capability ceiling
    3. ScopedToolRegistry defines current visible set
    4. on_tool_call_requested() is final runtime interceptor
    """
    result = list(tools)

    # PLAN mode: restrict to read-only observation tools
    if approval_mode == ApprovalMode.PLAN:
        result = [
            t for t in result
            if (
                t.meta.name in PLAN_MODE_ALLOWED_TOOLS
                or t.meta.category in PLAN_MODE_ALLOWED_CATEGORIES
            )
            and t.meta.name not in PLAN_MODE_BLOCKED_TOOLS
        ]

    # Filter by allowed categories
    if policy.allowed_tool_categories is not None:
        allowed = set(policy.allowed_tool_categories)
        result = [t for t in result if t.meta.category in allowed]

    # Filter by blocked categories
    if policy.blocked_tool_categories:
        blocked = set(policy.blocked_tool_categories)
        result = [t for t in result if t.meta.category not in blocked]

    # Filter network tools
    if not policy.allow_network_tools:
        result = [t for t in result if t.meta.category != "network"]

    # Filter system tools
    if not policy.allow_system_tools:
        result = [t for t in result if t.meta.category != "system"]

    # Filter spawn tools — both by name (legacy) and by delegation category
    if not policy.allow_spawn:
        result = [
            t for t in result
            if t.meta.name != "spawn_agent" and t.meta.category != "delegation"
        ]

    # Filter memory admin tools (§11.10)
    if not policy.allow_memory_admin:
        result = [t for t in result if t.meta.category not in ("memory", "memory_admin")]

    return result

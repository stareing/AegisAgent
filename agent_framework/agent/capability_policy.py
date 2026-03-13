from __future__ import annotations

from agent_framework.models.agent import CapabilityPolicy
from agent_framework.models.tool import ToolEntry


def apply_capability_policy(
    tools: list[ToolEntry], policy: CapabilityPolicy
) -> list[ToolEntry]:
    """Filter tools according to the capability policy.

    Priority (section 10.4):
    1. CapabilityPolicy defines the capability ceiling
    2. ScopedToolRegistry defines current visible set
    3. on_tool_call_requested() is final runtime interceptor
    """
    result = list(tools)

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

    # Filter spawn tools
    if not policy.allow_spawn:
        result = [t for t in result if t.meta.name != "spawn_agent"]

    return result

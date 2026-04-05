from __future__ import annotations

from agent_framework.models.agent import (
    AgentDefinition,
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
    agent_definition: AgentDefinition | None = None,
) -> list[ToolEntry]:
    """Filter tools according to the capability policy.

    Priority (section 10.4):
    1. ApprovalMode PLAN — restricts to read-only tools
    2. AgentDefinition — per-agent tool whitelist/blocklist (v4.0)
    3. CapabilityPolicy defines the capability ceiling
    4. ScopedToolRegistry defines current visible set
    5. on_tool_call_requested() is final runtime interceptor
    """
    result = list(tools)

    # PLAN mode: restrict to read-only observation tools
    # v4.1: Use is_read_only field as additional signal alongside category/name lists
    if approval_mode == ApprovalMode.PLAN:
        result = [
            t for t in result
            if (
                t.meta.name in PLAN_MODE_ALLOWED_TOOLS
                or t.meta.category in PLAN_MODE_ALLOWED_CATEGORIES
                or t.meta.is_read_only  # v4.1: tools declaring read-only are plan-safe
            )
            and t.meta.name not in PLAN_MODE_BLOCKED_TOOLS
            and not t.meta.is_destructive  # v4.1: destructive tools always blocked in plan
        ]

    # AgentDefinition tool filtering (v4.0)
    if agent_definition is not None:
        # Whitelist: if tools is specified, only those tools are available
        if agent_definition.tools is not None:
            allowed_names = set(agent_definition.tools)
            result = [t for t in result if t.meta.name in allowed_names]

        # Blocklist: disallowed_tools are always removed
        if agent_definition.disallowed_tools:
            blocked_names = set(agent_definition.disallowed_tools)
            result = [t for t in result if t.meta.name not in blocked_names]

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

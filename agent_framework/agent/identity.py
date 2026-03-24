"""Agent identity — configurable persona for agents.

Provides name, emoji, avatar, and description for each agent,
with preset identities for common roles and fallback resolution.
"""

from __future__ import annotations

from pydantic import BaseModel


class AgentIdentity(BaseModel):
    """Identity metadata for an agent."""

    model_config = {"frozen": True}

    name: str = "Agent"
    emoji: str = "🤖"
    avatar_url: str = ""
    description: str = ""


# Preset identities for common agent roles
DEFAULT_IDENTITIES: dict[str, AgentIdentity] = {
    "orchestrator": AgentIdentity(
        name="Orchestrator",
        emoji="🎯",
        description="Lead agent coordinating team tasks",
    ),
    "researcher": AgentIdentity(
        name="Researcher",
        emoji="🔬",
        description="Research and information gathering specialist",
    ),
    "coder": AgentIdentity(
        name="Coder",
        emoji="💻",
        description="Software development and code generation",
    ),
    "reviewer": AgentIdentity(
        name="Reviewer",
        emoji="📋",
        description="Code review and quality assurance",
    ),
    "writer": AgentIdentity(
        name="Writer",
        emoji="✍️",
        description="Documentation and content creation",
    ),
    "analyst": AgentIdentity(
        name="Analyst",
        emoji="📊",
        description="Data analysis and reporting",
    ),
    "devops": AgentIdentity(
        name="DevOps",
        emoji="⚙️",
        description="Infrastructure and deployment operations",
    ),
    "default": AgentIdentity(
        name="Agent",
        emoji="🤖",
        description="General-purpose AI agent",
    ),
}


def resolve_identity(
    agent_id: str,
    config_name: str = "",
    config_emoji: str = "",
    config_avatar: str = "",
) -> AgentIdentity:
    """Resolve agent identity with fallback chain.

    Priority: explicit config -> preset by agent_id -> preset by role keyword -> default.
    """
    # Build from config overrides
    if config_name or config_emoji or config_avatar:
        preset = DEFAULT_IDENTITIES.get(agent_id, DEFAULT_IDENTITIES["default"])
        return AgentIdentity(
            name=config_name or preset.name,
            emoji=config_emoji or preset.emoji,
            avatar_url=config_avatar or preset.avatar_url,
            description=preset.description,
        )

    # Try exact match
    if agent_id in DEFAULT_IDENTITIES:
        return DEFAULT_IDENTITIES[agent_id]

    # Try keyword match (e.g. "research_agent" matches "researcher")
    agent_lower = agent_id.lower()
    for role_key, identity in DEFAULT_IDENTITIES.items():
        if role_key in agent_lower:
            return identity

    return DEFAULT_IDENTITIES["default"]


def format_message_prefix(identity: AgentIdentity) -> str:
    """Format a message prefix for display (e.g. '[🎯 Orchestrator]')."""
    return f"[{identity.emoji} {identity.name}]"

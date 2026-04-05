"""Agent Team system — registry, plan management, shutdown coordination."""

from agent_framework.team.registry import TeamRegistry
from agent_framework.team.plan_registry import PlanRegistry
from agent_framework.team.shutdown_registry import ShutdownRegistry

__all__ = [
    "TeamRegistry",
    "PlanRegistry",
    "ShutdownRegistry",
]

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_framework.protocols.core import (
        ConfirmationHandlerProtocol,
        ContextEngineerProtocol,
        DelegationExecutorProtocol,
        MemoryManagerProtocol,
        ModelAdapterProtocol,
        SkillRouterProtocol,
        SubAgentRuntimeProtocol,
        ToolExecutorProtocol,
        ToolRegistryProtocol,
    )


@dataclass
class AgentRuntimeDeps:
    """Runtime dependency container for agent execution.

    Separates dependencies from BaseAgent so the agent focuses
    on strategy and hooks, not carrying all runtime references.
    """

    tool_registry: ToolRegistryProtocol
    tool_executor: ToolExecutorProtocol
    memory_manager: MemoryManagerProtocol
    context_engineer: ContextEngineerProtocol
    model_adapter: ModelAdapterProtocol
    skill_router: SkillRouterProtocol
    confirmation_handler: ConfirmationHandlerProtocol | None = None
    sub_agent_runtime: SubAgentRuntimeProtocol | None = None
    delegation_executor: DelegationExecutorProtocol | None = None

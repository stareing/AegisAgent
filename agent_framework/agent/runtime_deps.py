from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_framework.protocols.core import (ConfirmationHandlerProtocol,
                                                ContextEngineerProtocol,
                                                DelegationExecutorProtocol,
                                                MemoryManagerProtocol,
                                                ModelAdapterProtocol,
                                                SkillRouterProtocol,
                                                SubAgentRuntimeProtocol,
                                                ToolExecutorProtocol,
                                                ToolRegistryProtocol)


@dataclass
class AgentRuntimeDeps:
    """Runtime dependency container for agent execution.

    Separates dependencies from BaseAgent so the agent focuses
    on strategy and hooks, not carrying all runtime references.

    Object ownership scopes — do NOT violate these boundaries:

    PROCESS-LEVEL (shared across all runs, created once at bootstrap):
      - FrameworkConfig          — load_config() singleton
      - GlobalToolCatalog        — tool discovery index
      - Logger / EventBus        — observation infrastructure

    AGENT-LEVEL (shared across runs of the SAME agent, lives in this container):
      - tool_registry            — immutable after setup
      - tool_executor            — stateless routing, safe to share
      - memory_manager           — session-scoped internally (begin/end_session)
      - context_engineer         — shared instance, per-run injection via set_skill_context()
      - model_adapter            — stateless, safe to share
      - skill_router             — skill REGISTRY is shared; active skill is NOT stored here
      - confirmation_handler     — stateless policy executor
      - sub_agent_runtime        — shared spawning infrastructure
      - delegation_executor      — stateless routing

    RUN-LEVEL (created fresh per run, owned exclusively by RunCoordinator):
      - AgentState               — mutable run progress, NEVER reused across runs
      - SessionState             — mutable message history, NEVER reused across runs
      - EffectiveRunConfig       — frozen after construction, NEVER shared across runs

    SUB-AGENT RUN-LEVEL (created per sub-agent spawn, owned by SubAgentRuntime):
      - child AgentState         — isolated from parent
      - child SessionState       — isolated from parent
      - ScopedToolRegistry       — visibility subset of parent registry
      - scoped MemoryManager     — ISOLATED/INHERIT_READ/SHARED_WRITE (snapshot at spawn)

    Critical invariant: Run-level objects must NEVER leak into agent-level containers.
    If two concurrent runs share an AgentState or SessionState, the system is broken.
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
    hook_executor: Any = None

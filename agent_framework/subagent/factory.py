from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from agent_framework.agent.prompt_templates import SUB_AGENT_SYSTEM_PROMPT
from agent_framework.infra.logger import get_logger
from agent_framework.memory.sqlite_store import SQLiteMemoryStore
from agent_framework.models.memory import MemoryRecord
from agent_framework.models.subagent import MemoryScope, SubAgentSpec
from agent_framework.subagent.memory_scope import (
    InheritReadMemoryManager,
    IsolatedMemoryManager,
    SharedWriteMemoryManager,
)

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.agent.runtime_deps import AgentRuntimeDeps
    from agent_framework.protocols.core import MemoryManagerProtocol

logger = get_logger(__name__)


class SubAgentFactory:
    """Creates sub-agent instances with appropriate deps based on SubAgentSpec.

    Responsibilities:
    - Create agent config from spec overrides
    - Build scoped memory manager based on MemoryScope
    - Assemble AgentRuntimeDeps for the sub-agent
    - Apply tool whitelist filtering
    """

    def __init__(self, parent_deps: AgentRuntimeDeps) -> None:
        self._parent_deps = parent_deps

    def create_agent_and_deps(
        self,
        spec: SubAgentSpec,
        parent_agent: BaseAgent | None = None,
    ) -> tuple[BaseAgent, AgentRuntimeDeps]:
        """Create a sub-agent and its runtime deps from a SubAgentSpec."""
        from agent_framework.agent.default_agent import DefaultAgent
        from agent_framework.agent.runtime_deps import AgentRuntimeDeps
        from agent_framework.tools.executor import ToolExecutor
        from agent_framework.tools.registry import ScopedToolRegistry

        sub_agent_id = f"sub_{spec.spawn_id or uuid.uuid4().hex[:8]}"
        config_overrides = spec.agent_config_override or {}

        parent_model = parent_agent.agent_config.model_name if parent_agent else "gpt-3.5-turbo"
        default_prompt = SUB_AGENT_SYSTEM_PROMPT

        agent = DefaultAgent(
            agent_id=sub_agent_id,
            model_name=config_overrides.get("model_name", parent_model),
            system_prompt=config_overrides.get("system_prompt", default_prompt),
            temperature=config_overrides.get("temperature", 0.7),
            max_iterations=spec.max_iterations,
            # Section 14.2 & 20.2: SubAgentFactory MUST force allow_spawn_children=False
            allow_spawn_children=False,
        )

        # Build scoped memory manager
        memory_manager = self._build_memory_manager(
            spec.memory_scope,
            parent_agent,
        )

        # Build scoped tool registry
        # Doc 2.6/20.1: sub-agents default-deny system/network/subagent categories
        _BLOCKED_CATEGORIES = {"system", "network", "subagent"}

        all_tools = self._parent_deps.tool_registry.list_tools()

        if spec.tool_category_whitelist:
            # Explicit whitelist: only allow tools matching these categories
            allowed_names = [
                t.meta.name for t in all_tools
                if t.meta.category in spec.tool_category_whitelist
            ]
        else:
            # Default: allow all except blocked categories
            allowed_names = [
                t.meta.name for t in all_tools
                if t.meta.category not in _BLOCKED_CATEGORIES
            ]

        tool_registry = ScopedToolRegistry(
            source=self._parent_deps.tool_registry,
            whitelist=allowed_names,
        )

        # Build a scoped executor bound to this sub-agent and its scoped registry.
        parent_executor = self._parent_deps.tool_executor
        scoped_tool_executor = ToolExecutor(
            registry=tool_registry,
            confirmation_handler=self._parent_deps.confirmation_handler,
            delegation_executor=self._parent_deps.delegation_executor,
            mcp_client_manager=getattr(parent_executor, "_mcp", None),
            parent_agent_getter=lambda: agent,
            max_concurrent=getattr(parent_executor, "_max_concurrent", 5),
        )

        # Assemble deps
        deps = AgentRuntimeDeps(
            tool_registry=tool_registry,
            tool_executor=scoped_tool_executor,
            memory_manager=memory_manager,
            context_engineer=self._parent_deps.context_engineer,
            model_adapter=self._parent_deps.model_adapter,
            skill_router=self._parent_deps.skill_router,
            confirmation_handler=self._parent_deps.confirmation_handler,
            # Section 20.2: Sub-agents never get sub_agent_runtime (forced False above)
            sub_agent_runtime=None,
            delegation_executor=self._parent_deps.delegation_executor,
        )

        logger.info(
            "subagent.created",
            sub_agent_id=sub_agent_id,
            memory_scope=spec.memory_scope.value,
            max_iterations=spec.max_iterations,
        )

        return agent, deps

    def _build_memory_manager(
        self,
        scope: MemoryScope,
        parent_agent: BaseAgent | None,
    ) -> MemoryManagerProtocol:
        """Build a memory manager based on the specified scope."""
        parent_mm = self._parent_deps.memory_manager

        if scope == MemoryScope.SHARED_WRITE:
            # v2.4 §10: capture frozen snapshot of parent memories at spawn time
            snapshot = self._capture_parent_snapshot(parent_mm, parent_agent)
            return SharedWriteMemoryManager(
                parent_manager=parent_mm,
                parent_snapshot=snapshot,
            )

        if scope == MemoryScope.INHERIT_READ:
            # Create a local store for the sub-agent's own writes
            local_store = SQLiteMemoryStore(db_path=":memory:")
            # v2.4 §10: capture frozen snapshot of parent memories at spawn time
            snapshot = self._capture_parent_snapshot(parent_mm, parent_agent)
            mgr = InheritReadMemoryManager(
                store=local_store,
                parent_snapshot=snapshot,
            )
            return mgr

        # ISOLATED (default)
        local_store = SQLiteMemoryStore(db_path=":memory:")
        return IsolatedMemoryManager(store=local_store)

    def _capture_parent_snapshot(
        self,
        parent_mm: MemoryManagerProtocol,
        parent_agent: BaseAgent | None,
    ) -> list[MemoryRecord]:
        """v2.4 §10: Capture a frozen snapshot of parent memories at spawn time.

        Sub-agents read this snapshot throughout their run and do not see
        any subsequent changes to parent memory.
        """
        agent_id = parent_agent.agent_id if parent_agent else ""
        return parent_mm.list_memories(agent_id, None)

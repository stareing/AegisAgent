"""Entry point: AgentFramework class wiring all components together."""

from __future__ import annotations

from typing import Any

from agent_framework.adapters.model.litellm_adapter import LiteLLMAdapter
from agent_framework.agent.coordinator import RunCoordinator
from agent_framework.agent.default_agent import DefaultAgent
from agent_framework.agent.runtime_deps import AgentRuntimeDeps
from agent_framework.agent.skill_router import SkillRouter
from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.engineer import ContextEngineer
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.infra.config import FrameworkConfig, load_config
from agent_framework.infra.logger import configure_logging, get_logger
from agent_framework.memory.default_manager import DefaultMemoryManager
from agent_framework.memory.sqlite_store import SQLiteMemoryStore
from agent_framework.models.agent import AgentRunResult
from agent_framework.tools.catalog import GlobalToolCatalog
from agent_framework.tools.confirmation import AutoApproveConfirmationHandler, CLIConfirmationHandler
from agent_framework.tools.delegation import DelegationExecutor
from agent_framework.tools.executor import ToolExecutor
from agent_framework.tools.registry import ToolRegistry

logger = get_logger(__name__)


class AgentFramework:
    """Top-level facade that wires all framework components.

    Usage:
        framework = AgentFramework()
        framework.setup()
        result = await framework.run("Hello, solve this problem...")
    """

    def __init__(
        self,
        config: FrameworkConfig | None = None,
        config_path: str | None = None,
    ) -> None:
        self.config = config or load_config(config_path)
        self._catalog = GlobalToolCatalog()
        self._registry: ToolRegistry | None = None
        self._coordinator: RunCoordinator | None = None
        self._deps: AgentRuntimeDeps | None = None
        self._agent: Any = None
        self._mcp_manager: Any = None
        self._a2a_adapter: Any = None
        self._setup_done = False

    def setup(
        self,
        agent: Any = None,
        auto_approve_tools: bool = False,
    ) -> None:
        """Initialize all components and wire dependencies."""
        configure_logging(
            json_output=self.config.logging.json_output,
            level=self.config.logging.level,
        )

        # Memory
        memory_store = SQLiteMemoryStore(db_path=self.config.memory.db_path)
        memory_manager = DefaultMemoryManager(
            store=memory_store,
            max_memories_in_context=self.config.memory.max_memories_in_context,
            auto_extract=self.config.memory.auto_extract_memory,
        )

        # Model adapter
        model_adapter = LiteLLMAdapter(
            model_name=self.config.model.default_model_name,
            timeout_ms=self.config.model.timeout_ms,
            max_retries=self.config.model.max_retries,
        )

        # Tool registry
        self._registry = ToolRegistry()
        for entry in self._catalog.list_all():
            self._registry.register(entry)

        # Confirmation handler
        if auto_approve_tools:
            confirmation = AutoApproveConfirmationHandler()
        else:
            confirmation = CLIConfirmationHandler()

        # Delegation executor (placeholder, wired after subagent runtime)
        delegation_executor = DelegationExecutor(sub_agent_runtime=None)

        # Tool executor
        tool_executor = ToolExecutor(
            registry=self._registry,
            confirmation_handler=confirmation,
            delegation_executor=delegation_executor,
            mcp_client_manager=self._mcp_manager,
            max_concurrent=self.config.tools.max_concurrent_tool_calls,
        )

        # Context
        source_provider = ContextSourceProvider()
        builder = ContextBuilder(
            max_context_tokens=self.config.context.max_context_tokens,
            reserve_for_output=self.config.context.reserve_for_output,
        )
        compressor = ContextCompressor()
        context_engineer = ContextEngineer(
            source_provider=source_provider,
            builder=builder,
            compressor=compressor,
        )

        # Skill router
        skill_router = SkillRouter()

        # Assemble deps
        self._deps = AgentRuntimeDeps(
            tool_registry=self._registry,
            tool_executor=tool_executor,
            memory_manager=memory_manager,
            context_engineer=context_engineer,
            model_adapter=model_adapter,
            skill_router=skill_router,
            confirmation_handler=confirmation,
            delegation_executor=delegation_executor,
        )

        # Wire SubAgent Runtime
        try:
            from agent_framework.subagent.runtime import SubAgentRuntime

            sub_runtime = SubAgentRuntime(
                parent_deps=self._deps,
                max_concurrent=self.config.subagent.max_concurrent_sub_agents,
                max_per_run=self.config.subagent.max_sub_agents_per_run,
            )
            self._deps.sub_agent_runtime = sub_runtime
            delegation_executor._sub_agent_runtime = sub_runtime
        except Exception as e:
            logger.warning("subagent_runtime.init_failed", error=str(e))

        # Agent — main agent CAN spawn children (sub-agents can't, enforced by factory)
        self._agent = agent or DefaultAgent(
            model_name=self.config.model.default_model_name,
            temperature=self.config.model.temperature,
            allow_spawn_children=True,
        )

        self._coordinator = RunCoordinator()
        self._setup_done = True

        logger.info(
            "framework.setup_complete",
            model=self.config.model.default_model_name,
            tools_count=len(self._registry.list_tools()),
        )

    async def setup_mcp(self) -> None:
        """Connect to configured MCP servers and sync tools."""
        from agent_framework.models.mcp import MCPServerConfig
        from agent_framework.protocols.mcp.mcp_client_manager import MCPClientManager

        self._mcp_manager = MCPClientManager()

        for server_dict in self.config.mcp.servers:
            server_config = MCPServerConfig(**server_dict)
            await self._mcp_manager.connect_server(server_config)

        if self._catalog:
            self._mcp_manager.sync_tools_to_catalog(self._catalog)
            # Re-sync to registry
            if self._registry:
                for entry in self._catalog.list_all():
                    if entry.meta.source == "mcp" and not self._registry.has_tool(entry.meta.name):
                        self._registry.register(entry)

        # Update executor's mcp reference
        if self._deps and hasattr(self._deps.tool_executor, "_mcp"):
            self._deps.tool_executor._mcp = self._mcp_manager

    async def setup_a2a(self) -> None:
        """Discover configured A2A agents and register their tools."""
        from agent_framework.protocols.a2a.a2a_client_adapter import A2AClientAdapter

        self._a2a_adapter = A2AClientAdapter()

        for agent_dict in self.config.a2a.known_agents:
            url = agent_dict.get("url", "")
            alias = agent_dict.get("alias")
            if url:
                await self._a2a_adapter.discover_agent(url, alias)

        if self._catalog:
            self._a2a_adapter.sync_agents_to_catalog(self._catalog)
            if self._registry:
                for entry in self._catalog.list_all():
                    if entry.meta.source == "a2a" and not self._registry.has_tool(entry.meta.name):
                        self._registry.register(entry)

    def register_tool(self, func: Any) -> None:
        """Register a @tool decorated function."""
        self._catalog.register_function(func)
        if self._registry:
            entry = self._catalog.get(f"local::{func.__tool_meta__.name}")
            if entry:
                self._registry.register(entry)

    def register_tools_from_module(self, module: Any) -> int:
        """Register all @tool functions from a module."""
        count = self._catalog.register_module(module)
        if self._registry:
            for entry in self._catalog.list_all():
                if not self._registry.has_tool(entry.meta.name):
                    self._registry.register(entry)
        return count

    async def run(self, task: str) -> AgentRunResult:
        """Run the agent on a task."""
        if not self._setup_done:
            self.setup()
        return await self._coordinator.run(self._agent, self._deps, task)

    async def shutdown(self) -> None:
        """Clean up resources."""
        if self._mcp_manager:
            await self._mcp_manager.disconnect_all()
        logger.info("framework.shutdown")

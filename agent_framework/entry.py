"""Entry point: AgentFramework class wiring all components together.

Framework vs Integration Layer boundary (v2.5.2 §24):

FRAMEWORK CORE (this package) is responsible for:
- Agent runtime (loop, coordinator, state)
- Tool execution and routing
- Context engineering
- Memory management
- Sub-agent orchestration

INTEGRATION LAYER (external consumers) is responsible for:
- User authentication and authorization
- Session ID generation and mapping
- API DTO conversion (REST/WebSocket/gRPC)
- UI rendering and display
- Memory admin tool exposure strategy
- Deployment-specific confirmation policies

Bypass prohibition (v2.5.2 §24):
- Integration code MUST call AgentFramework public methods (run, register_tool, etc.)
- Integration code MUST NOT directly access internal components:
  - coordinator.run() → use framework.run()
  - tool_executor.execute() → use framework.register_tool() + framework.run()
  - memory_manager.remember() → use framework.list_memories() etc.
  - context_engineer.prepare_context_for_llm() → internal only
- Internal deps (self._deps, self._coordinator) are implementation details.
  Their types and wiring may change without notice.
- This boundary ensures the framework core is not coupled to any specific
  deployment model (CLI, REST API, SDK, REPL).
"""

from __future__ import annotations

from typing import Any

from agent_framework.adapters.model.base_adapter import BaseModelAdapter
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
from agent_framework.models.agent import AgentRunResult, Skill
from agent_framework.models.message import Message
from agent_framework.models.session import SessionState
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
        # Integration-layer active skill tracking for interactive UIs.
        # This is NOT the run-scoped active skill — it's a UI convenience
        # that the interactive terminal uses between runs.
        self._interactive_active_skill: Any = None

    def setup(
        self,
        agent: Any = None,
        auto_approve_tools: bool = False,
    ) -> None:
        """Initialize all components and wire dependencies."""
        configure_logging(self.config.logging)

        # Memory
        memory_store = SQLiteMemoryStore(db_path=self.config.memory.db_path)
        memory_manager = DefaultMemoryManager(
            store=memory_store,
            max_memories_in_context=self.config.memory.max_memories_in_context,
            auto_extract=self.config.memory.auto_extract_memory,
        )

        # Model adapter
        model_adapter = self._create_model_adapter()

        # Register built-in tools (filesystem, system, spawn_agent)
        from agent_framework.tools.builtin import register_all_builtins
        register_all_builtins(self._catalog)

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
            parent_agent_getter=lambda: self._agent,
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

        # Skill router — load declarative skills from config
        skill_router = SkillRouter()
        for skill_def in self.config.skills.definitions:
            from agent_framework.models.agent import Skill
            skill_router.register_skill(Skill(
                skill_id=skill_def.skill_id,
                name=skill_def.name,
                description=skill_def.description,
                trigger_keywords=skill_def.trigger_keywords,
                system_prompt_addon=skill_def.system_prompt_addon,
                model_override=skill_def.model_override,
                temperature_override=skill_def.temperature_override,
            ))

        # Load file-based skills from directories (SKILL.md)
        import pathlib
        skill_dirs: list[pathlib.Path] = []
        project_skills = pathlib.Path.cwd() / "skills"
        if project_skills.is_dir():
            skill_dirs.append(project_skills)
        user_skills = pathlib.Path.home() / ".agent" / "skills"
        if user_skills.is_dir():
            skill_dirs.append(user_skills)
        for extra_dir in self.config.skills.directories:
            p = pathlib.Path(extra_dir)
            if p.is_dir():
                skill_dirs.append(p)
        if skill_dirs:
            file_count = skill_router.load_file_skills(skill_dirs)
            logger.info("skills.file_loaded", count=file_count,
                        dirs=[str(d) for d in skill_dirs])

        # Wire invoke_skill tool with runtime references
        from agent_framework.tools.builtin_skills import set_skill_runtime
        set_skill_runtime(skill_router, context_engineer)

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

        # Agent — use OrchestratorAgent (orchestration-aware prompt + spawn enabled)
        # Sub-agents use DefaultAgent with allow_spawn_children=False (enforced by factory)
        if agent:
            self._agent = agent
        else:
            from agent_framework.agent.orchestrator_agent import OrchestratorAgent
            self._agent = OrchestratorAgent(
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

    def _create_model_adapter(self) -> BaseModelAdapter:
        """Create model adapter based on config.model.adapter_type.

        SDK imports are lazy — only the selected adapter's SDK is loaded.
        """
        cfg = self.config.model
        common = {
            "model_name": cfg.default_model_name,
            "timeout_ms": cfg.timeout_ms,
            "max_retries": cfg.max_retries,
        }
        if cfg.api_key:
            common["api_key"] = cfg.api_key
        if cfg.api_base:
            common["api_base"] = cfg.api_base
        if cfg.max_output_tokens:
            common["max_output_tokens"] = cfg.max_output_tokens

        match cfg.adapter_type:
            case "openai":
                from agent_framework.adapters.model.openai_adapter import OpenAIAdapter
                return OpenAIAdapter(**common)
            case "anthropic":
                from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter
                return AnthropicAdapter(**common)
            case "google":
                from agent_framework.adapters.model.google_adapter import GoogleAdapter
                common.pop("api_base", None)  # google-genai doesn't use api_base
                return GoogleAdapter(**common)
            case "deepseek":
                from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter
                return DeepSeekAdapter(**common)
            case "doubao":
                from agent_framework.adapters.model.openai_compatible_adapter import DoubaoAdapter
                return DoubaoAdapter(**common)
            case "qwen":
                from agent_framework.adapters.model.openai_compatible_adapter import QwenAdapter
                return QwenAdapter(**common)
            case "zhipu":
                from agent_framework.adapters.model.openai_compatible_adapter import ZhipuAdapter
                return ZhipuAdapter(**common)
            case "minimax":
                from agent_framework.adapters.model.openai_compatible_adapter import MiniMaxAdapter
                return MiniMaxAdapter(**common)
            case "custom":
                from agent_framework.adapters.model.openai_compatible_adapter import CustomAdapter
                return CustomAdapter(**common)
            case _:
                from agent_framework.adapters.model.litellm_adapter import LiteLLMAdapter
                common.pop("api_key", None)  # litellm reads API keys from env
                return LiteLLMAdapter(**common)

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
        if self._deps and self._deps.delegation_executor:
            self._deps.delegation_executor.set_a2a_adapter(self._a2a_adapter)

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

    def list_memories(self, user_id: str | None = None) -> list:
        """List saved memories for the current agent."""
        if not self._setup_done:
            self.setup()
        return self._deps.memory_manager.list_memories(self._agent.agent_id, user_id)

    def forget_memory(self, memory_id: str) -> None:
        """Delete one saved memory by id."""
        if not self._setup_done:
            self.setup()
        self._deps.memory_manager.forget(memory_id)

    def pin_memory(self, memory_id: str) -> None:
        if not self._setup_done:
            self.setup()
        self._deps.memory_manager.pin(memory_id)

    def unpin_memory(self, memory_id: str) -> None:
        if not self._setup_done:
            self.setup()
        self._deps.memory_manager.unpin(memory_id)

    def activate_memory(self, memory_id: str) -> None:
        if not self._setup_done:
            self.setup()
        self._deps.memory_manager.activate(memory_id)

    def deactivate_memory(self, memory_id: str) -> None:
        if not self._setup_done:
            self.setup()
        self._deps.memory_manager.deactivate(memory_id)

    def clear_memories(self, user_id: str | None = None) -> int:
        if not self._setup_done:
            self.setup()
        return self._deps.memory_manager.clear_memories(self._agent.agent_id, user_id)

    def set_memory_enabled(self, enabled: bool) -> None:
        if not self._setup_done:
            self.setup()
        self._deps.memory_manager.set_enabled(enabled)

    # ---------------------------------------------------------------
    # Skill public API
    # ---------------------------------------------------------------

    def register_skill(self, skill: Skill) -> None:
        """Register a skill for keyword-based activation."""
        if not self._setup_done:
            self.setup()
        self._deps.skill_router.register_skill(skill)
        logger.info("skill.registered", skill_id=skill.skill_id, name=skill.name)

    def list_skills(self) -> list[Skill]:
        """List all registered skills."""
        if not self._setup_done:
            self.setup()
        return self._deps.skill_router.list_skills()

    def remove_skill(self, skill_id: str) -> bool:
        """Remove a skill by ID. Returns True if found and removed."""
        if not self._setup_done:
            self.setup()
        router = self._deps.skill_router
        if skill_id in router._skills:
            del router._skills[skill_id]
            logger.info("skill.removed", skill_id=skill_id)
            return True
        return False

    def get_active_skill(self) -> Skill | None:
        """Return the interactive-mode active skill, if any.

        This is an INTEGRATION LAYER convenience for interactive UIs.
        During an actual run, active skill is managed as a run-scoped
        local in RunCoordinator, not here.
        """
        return self._interactive_active_skill

    def activate_skill(self, skill: Skill) -> None:
        """Activate a skill in interactive mode (UI convenience)."""
        self._interactive_active_skill = skill
        if self._deps:
            self._deps.context_engineer.set_skill_context(skill.system_prompt_addon)

    def deactivate_skill(self) -> None:
        """Deactivate the current interactive skill."""
        self._interactive_active_skill = None
        if self._deps:
            self._deps.context_engineer.set_skill_context(None)

    # ---------------------------------------------------------------
    # Tool public API
    # ---------------------------------------------------------------

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

    async def run(
        self,
        task: str,
        initial_session_messages: list[Message] | None = None,
    ) -> AgentRunResult:
        """Run the agent on a task.

        Args:
            task: The user input / task description.
            initial_session_messages: Prior conversation history. Injected into
                the session so the model sees multi-turn context. The ContextBuilder
                automatically trims older messages when the token budget is tight.
        """
        if not self._setup_done:
            self.setup()
        return await self._coordinator.run(
            self._agent,
            self._deps,
            task,
            initial_session_messages=initial_session_messages,
        )

    async def shutdown(self) -> None:
        """Clean up resources."""
        if self._mcp_manager:
            await self._mcp_manager.disconnect_all()
        logger.info("framework.shutdown")

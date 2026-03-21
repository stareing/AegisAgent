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

import asyncio
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
from agent_framework.models.message import ContentPart, Message
from agent_framework.models.session import SessionState
from agent_framework.subagent.delegation import DelegationExecutor
from agent_framework.tools.catalog import GlobalToolCatalog
from agent_framework.tools.confirmation import (AutoApproveConfirmationHandler,
                                                CLIConfirmationHandler)
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
        self._memory_store: SQLiteMemoryStore | None = None
        # Integration-layer active skill tracking for interactive UIs.
        # This is NOT the run-scoped active skill — it's a UI convenience
        # that the interactive terminal uses between runs.
        self._interactive_active_skill: Any = None

    def setup(
        self,
        agent: Any = None,
        auto_approve_tools: bool = False,
        model_adapter: BaseModelAdapter | None = None,
    ) -> None:
        """Initialize all components and wire dependencies."""
        configure_logging(self.config.logging)

        # Tracing (noop when disabled or SDK absent)
        from agent_framework.infra.telemetry import get_tracing_manager
        get_tracing_manager().configure(self.config.tracing)

        # Hooks subsystem — instance-level registry and executor
        from agent_framework.hooks.singleton import HookSubsystem
        self._hook_subsystem = HookSubsystem()
        self._hook_registry = self._hook_subsystem.registry
        self._hook_executor = self._hook_subsystem.executor
        self._hook_dispatcher = self._hook_subsystem.dispatcher

        # CONFIG_LOADED hook — fires after config is available, before component init
        from agent_framework.hooks.payloads import config_loaded_payload
        from agent_framework.models.hook import HookPoint
        try:
            self._hook_dispatcher.fire_sync_advisory(
                HookPoint.CONFIG_LOADED,
                payload=config_loaded_payload(
                    self.config.model.adapter_type,
                    getattr(self.config.memory, "store_type", "sqlite"),
                ),
            )
        except Exception:
            pass

        # Memory
        memory_store = _create_memory_store(self.config.memory)
        self._memory_store = memory_store
        memory_manager = DefaultMemoryManager(
            store=memory_store,
            max_memories_in_context=self.config.memory.max_memories_in_context,
            auto_extract=self.config.memory.auto_extract_memory,
            hook_executor=self._hook_executor,
        )
        from agent_framework.models.agent import MemoryQuota
        memory_manager.set_quota(MemoryQuota(
            max_items_per_user=self.config.memory.max_memory_items_per_user,
        ))

        # Model adapter
        model_adapter = model_adapter or self._create_model_adapter()

        # Apply session mode from config
        if hasattr(model_adapter, "_session_mode_config"):
            model_adapter._session_mode_config = self.config.model.session_mode

        # Register built-in tools (filesystem, system, spawn_agent, memory_admin)
        from agent_framework.tools.builtin import register_all_builtins
        register_all_builtins(
            self._catalog,
            shell_enabled=self.config.tools.shell_enabled,
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

        # Interaction channel for long-term parent-child delegation (v3.1)
        from agent_framework.subagent.hitl import QueueHITLHandler
        from agent_framework.subagent.interaction_channel import (
            InMemoryInteractionChannel, SQLiteInteractionChannel,
        )

        li_cfg = self.config.long_interaction
        if li_cfg.channel_backend == "sqlite":
            self._interaction_channel = SQLiteInteractionChannel(
                db_path=li_cfg.channel_db_path,
                max_events_per_spawn=li_cfg.max_delegation_events_per_subagent,
            )
        else:
            self._interaction_channel = InMemoryInteractionChannel(
                max_events_per_spawn=li_cfg.max_delegation_events_per_subagent,
            )
        self._hitl_handler = QueueHITLHandler(
            max_pending_per_run=self.config.long_interaction.max_pending_hitl_requests_per_run,
        )

        # Delegation executor (placeholder, wired after subagent runtime)
        delegation_executor = DelegationExecutor(
            sub_agent_runtime=None,
            hook_executor=self._hook_executor,
            confirmation_handler=confirmation,
            interaction_channel=self._interaction_channel,
            hitl_handler=self._hitl_handler,
        )

        # Tool executor
        tool_executor = ToolExecutor(
            registry=self._registry,
            confirmation_handler=confirmation,
            delegation_executor=delegation_executor,
            mcp_client_manager=self._mcp_manager,
            parent_agent_getter=lambda: self._agent,
            max_concurrent=self.config.tools.max_concurrent_tool_calls,
            default_collection_strategy=self.config.subagent.default_collection_strategy,
            collection_poll_interval_ms=self.config.subagent.collection_poll_interval_ms,
            hook_executor=self._hook_executor,
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
            hook_executor=self._hook_executor,
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

        # INSTRUCTIONS_LOADED hook — fires after skills/tools are assembled
        from agent_framework.hooks.payloads import instructions_loaded_payload
        try:
            self._hook_dispatcher.fire_sync_advisory(
                HookPoint.INSTRUCTIONS_LOADED,
                payload=instructions_loaded_payload(
                    skills_loaded=len(skill_router.list_skills()),
                    tools_registered=len(self._registry.list_tools()) if self._registry else 0,
                ),
            )
        except Exception:
            pass

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
            hook_executor=self._hook_executor,
        )

        # Wire SubAgent Runtime
        try:
            from agent_framework.subagent.runtime import SubAgentRuntime

            sub_runtime = SubAgentRuntime(
                parent_deps=self._deps,
                max_concurrent=self.config.subagent.max_concurrent_sub_agents,
                max_per_run=self.config.subagent.max_sub_agents_per_run,
                max_spawn_depth=self.config.subagent.max_spawn_depth,
                live_agent_ttl_seconds=self.config.subagent.live_agent_ttl_seconds,
                max_live_agents_per_run=self.config.subagent.max_live_agents_per_run,
                dynamic_pool=self.config.subagent.dynamic_pool,
                min_concurrent=self.config.subagent.min_concurrent,
                max_concurrent_ceiling=self.config.subagent.max_concurrent_ceiling,
            )
            self._deps.sub_agent_runtime = sub_runtime
            delegation_executor._sub_agent_runtime = sub_runtime

            # Wire checkpoint store for persistent state snapshots
            try:
                from agent_framework.subagent.checkpoint import SQLiteCheckpointStore
                sub_runtime._checkpoint_store = SQLiteCheckpointStore()
            except Exception:
                pass  # Checkpoint is optional; degrades gracefully

            # Wire stream sink: child events → tool executor queue → parent stream
            def _child_stream_sink(spawn_id: str, event: Any) -> None:
                from agent_framework.models.stream import StreamEvent, StreamEventType
                wrapped = StreamEvent(
                    type=StreamEventType.SUBAGENT_STREAM,
                    data={"spawn_id": spawn_id, "event_type": event.type.value, **event.data},
                )
                tool_executor.enqueue_child_stream_event(wrapped)

            sub_runtime._stream_sink = _child_stream_sink
        except Exception as e:
            logger.warning("subagent_runtime.init_failed", error=str(e))

        # Agent — use OrchestratorAgent (orchestration-aware prompt + spawn enabled)
        # Sub-agents use DefaultAgent with allow_spawn_children=False (enforced by factory)
        if agent:
            self._agent = agent
        else:
            from agent_framework.agent.orchestrator_agent import \
                OrchestratorAgent
            self._agent = OrchestratorAgent(
                model_name=self.config.model.default_model_name,
                temperature=self.config.model.temperature,
                max_output_tokens=self.config.model.max_output_tokens,
                allow_spawn_children=True,
                max_concurrent_tool_calls=self.config.tools.max_concurrent_tool_calls,
                allow_parallel_tool_calls=self.config.tools.allow_parallel_tool_calls,
                progressive_tool_results=(self.config.subagent.execution_mode == "progressive"),
            )

        # Bind config-sourced policies to agent so run-level policy
        # reflects FrameworkConfig rather than BaseAgent defaults.
        self._bind_config_policies(self._agent)

        # Bind memory context for memory_admin tools — AFTER agent creation
        # so agent_id matches the actual agent (e.g. "orchestrator" not "default")
        from agent_framework.tools.builtin.memory_admin import \
            set_memory_context
        set_memory_context(memory_manager, self._agent.agent_id)

        self._coordinator = RunCoordinator()

        # Wire interaction channel into coordinator's notification channel (v3.1)
        self._coordinator._notification_channel.set_interaction_channel(
            self._interaction_channel
        )

        # Discover and auto-initialize team from .agent-team/
        self._discovered_teams: list[dict] = []
        try:
            import pathlib
            from agent_framework.team.loader import discover_teams

            team_dirs: list[pathlib.Path] = []
            project_teams = pathlib.Path.cwd() / ".agent-team"
            if project_teams.is_dir():
                team_dirs.append(project_teams)
            user_teams = pathlib.Path.home() / ".agent" / "teams"
            if user_teams.is_dir():
                team_dirs.append(user_teams)
            if team_dirs:
                self._discovered_teams = discover_teams(team_dirs)
                if self._discovered_teams:
                    logger.info(
                        "teams.discovered",
                        count=len(self._discovered_teams),
                        names=[t["team_id"] for t in self._discovered_teams],
                    )
                    try:
                        self._auto_init_team()
                    except Exception as te:
                        logger.warning("teams.auto_init_failed", error=str(te))
        except Exception:
            pass

        self._setup_done = True

        logger.info(
            "framework.setup_complete",
            model=self.config.model.default_model_name,
            tools_count=len(self._registry.list_tools()),
        )

    def _auto_init_team(self) -> None:
        """Auto-initialize team from discovered .agent-team/ definitions."""
        import uuid
        from agent_framework.notification.bus import AgentBus
        from agent_framework.notification.persistence import InMemoryBusPersistence
        from agent_framework.team.registry import TeamRegistry
        from agent_framework.team.plan_registry import PlanRegistry
        from agent_framework.team.shutdown_registry import ShutdownRegistry
        from agent_framework.team.mailbox import TeamMailbox
        from agent_framework.team.coordinator import TeamCoordinator

        team_id = f"team_{uuid.uuid4().hex[:8]}"
        bus = AgentBus(persistence=InMemoryBusPersistence())
        registry = TeamRegistry(team_id)
        mailbox = TeamMailbox(bus, registry)

        lead_id = self._agent.agent_id if self._agent else "lead"
        runtime = getattr(self._deps, "sub_agent_runtime", None)

        coordinator = TeamCoordinator(
            team_id=team_id,
            lead_agent_id=lead_id,
            mailbox=mailbox,
            team_registry=registry,
            plan_registry=PlanRegistry(),
            shutdown_registry=ShutdownRegistry(),
            sub_agent_runtime=runtime,
        )
        coordinator.create_team("auto")

        # Register discovered role definitions for tool whitelist
        for role_def in self._discovered_teams:
            role_name = role_def.get("team_id", "")
            fm = role_def.get("frontmatter", {})
            if role_name:
                coordinator.register_role_definition(role_name, fm)

        # Wire into tool executor
        executor = self._deps.tool_executor
        executor._team_coordinator = coordinator
        executor._team_mailbox = mailbox
        executor._current_agent_role = "lead"
        executor._current_team_id = team_id
        executor._current_spawn_id = lead_id

        # Auto-spawn all discovered roles as teammates
        import asyncio

        async def _spawn_all() -> None:
            for role_def in self._discovered_teams:
                role_name = role_def.get("team_id", "")
                desc = role_def.get("frontmatter", {}).get("description", "Ready")
                # Standby task — agent reports ready and goes IDLE.
                # Real work happens when assign_task() is called.
                task = f"You are '{role_name}'. {desc} Report ready and wait."
                try:
                    await coordinator.spawn_teammate(role=role_name, task_input=task)
                except Exception as e:
                    logger.warning("teams.auto_spawn_failed", role=role_name, error=str(e))
            # Store raw definitions for assign_task to look up body
            coordinator._discovered_teams_raw = self._discovered_teams

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_spawn_all())
            else:
                loop.run_until_complete(_spawn_all())
        except RuntimeError:
            asyncio.run(_spawn_all())

        # Reset quota — auto-spawned team members don't count toward
        # the per-run sub-agent limit. Only user-initiated spawns count.
        if runtime is not None:
            runtime._scheduler._run_counts.pop(team_id, None)

        logger.info(
            "teams.auto_initialized",
            team_id=team_id, lead=lead_id,
            roles=[t["team_id"] for t in self._discovered_teams],
            auto_spawned=True,
        )

    def _bind_config_policies(self, agent: Any) -> None:
        """Override agent's default policy methods with config-sourced values.

        Without this, BaseAgent.get_memory_policy() returns MemoryPolicy()
        defaults (all True, max_in_context=10), which would overwrite
        the MemoryConfig values injected at setup time on every run.
        """
        from agent_framework.models.agent import ContextPolicy, MemoryPolicy
        mem_cfg = self.config.memory
        ctx_cfg = self.config.context

        config_memory_policy = MemoryPolicy(
            memory_enabled=mem_cfg.enable_saved_memory,
            auto_extract=mem_cfg.auto_extract_memory,
            max_in_context=mem_cfg.max_memories_in_context,
        )
        config_context_policy = ContextPolicy(
            allow_compression=(ctx_cfg.default_compression_strategy != "NONE"),
        )

        # Patch methods on the agent instance (not the class)
        agent.get_memory_policy = lambda _state: config_memory_policy
        agent.get_context_policy = lambda _state: config_context_policy

    def _create_model_adapter(self) -> BaseModelAdapter:
        """Create model adapter based on config.model.adapter_type.

        SDK imports are lazy — only the selected adapter's SDK is loaded.
        If fallback_models are configured, wraps the primary adapter
        in a FallbackModelAdapter that tries alternatives on failure.
        """
        cfg = self.config.model
        primary = self._build_adapter(
            adapter_type=cfg.adapter_type,
            model_name=cfg.default_model_name,
            timeout_ms=cfg.timeout_ms,
            max_retries=cfg.max_retries,
            api_key=cfg.api_key,
            api_base=cfg.api_base,
            max_output_tokens=cfg.max_output_tokens,
        )

        if not cfg.fallback_models:
            return primary

        from agent_framework.adapters.model.fallback_adapter import \
            FallbackModelAdapter

        fallbacks: list[BaseModelAdapter] = []
        for fb_dict in cfg.fallback_models:
            fallbacks.append(self._build_adapter(
                adapter_type=fb_dict.get("adapter_type", cfg.adapter_type),
                model_name=fb_dict.get("default_model_name", cfg.default_model_name),
                timeout_ms=fb_dict.get("timeout_ms", cfg.timeout_ms),
                max_retries=fb_dict.get("max_retries", cfg.max_retries),
                api_key=fb_dict.get("api_key", cfg.api_key),
                api_base=fb_dict.get("api_base", cfg.api_base),
                max_output_tokens=fb_dict.get("max_output_tokens", cfg.max_output_tokens),
            ))

        logger.info(
            "model.fallback_chain_created",
            primary=cfg.adapter_type,
            fallback_count=len(fallbacks),
        )
        return FallbackModelAdapter(primary=primary, fallbacks=fallbacks)

    @staticmethod
    def _build_adapter(
        *,
        adapter_type: str,
        model_name: str,
        timeout_ms: int,
        max_retries: int,
        api_key: str | None,
        api_base: str | None,
        max_output_tokens: int,
    ) -> BaseModelAdapter:
        """Build a single adapter instance from explicit parameters."""
        common: dict[str, object] = {
            "model_name": model_name,
            "timeout_ms": timeout_ms,
            "max_retries": max_retries,
        }
        if api_key:
            common["api_key"] = api_key
        if api_base:
            common["api_base"] = api_base
        if max_output_tokens:
            common["max_output_tokens"] = max_output_tokens

        match adapter_type:
            case "openai":
                from agent_framework.adapters.model.openai_adapter import \
                    OpenAIAdapter
                return OpenAIAdapter(**common)
            case "anthropic":
                from agent_framework.adapters.model.anthropic_adapter import \
                    AnthropicAdapter
                return AnthropicAdapter(**common)
            case "google":
                from agent_framework.adapters.model.google_adapter import \
                    GoogleAdapter
                common.pop("api_base", None)
                return GoogleAdapter(**common)
            # --- International OpenAI-compatible providers ---
            case "openrouter":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    OpenRouterAdapter
                return OpenRouterAdapter(**common)
            case "together":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    TogetherAdapter
                return TogetherAdapter(**common)
            case "groq":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    GroqAdapter
                return GroqAdapter(**common)
            case "fireworks":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    FireworksAdapter
                return FireworksAdapter(**common)
            case "mistral":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    MistralAdapter
                return MistralAdapter(**common)
            case "perplexity":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    PerplexityAdapter
                return PerplexityAdapter(**common)
            # --- Chinese providers ---
            case "deepseek":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    DeepSeekAdapter
                return DeepSeekAdapter(**common)
            case "doubao":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    DoubaoAdapter
                return DoubaoAdapter(**common)
            case "qwen":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    QwenAdapter
                return QwenAdapter(**common)
            case "zhipu":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    ZhipuAdapter
                return ZhipuAdapter(**common)
            case "minimax":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    MiniMaxAdapter
                return MiniMaxAdapter(**common)
            case "siliconflow":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    SiliconFlowAdapter
                return SiliconFlowAdapter(**common)
            case "moonshot":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    MoonshotAdapter
                return MoonshotAdapter(**common)
            case "baichuan":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    BaichuanAdapter
                return BaichuanAdapter(**common)
            case "yi":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    YiAdapter
                return YiAdapter(**common)
            case "custom":
                from agent_framework.adapters.model.openai_compatible_adapter import \
                    CustomAdapter
                return CustomAdapter(**common)
            case _:
                from agent_framework.adapters.model.litellm_adapter import \
                    LiteLLMAdapter
                common.pop("api_key", None)
                return LiteLLMAdapter(**common)

    async def setup_mcp(self) -> None:
        """Connect to configured MCP servers and sync tools."""
        from agent_framework.models.mcp import MCPServerConfig
        from agent_framework.protocols.mcp.mcp_client_manager import \
            MCPClientManager

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
        from agent_framework.protocols.a2a.a2a_client_adapter import \
            A2AClientAdapter
        from agent_framework.protocols.a2a.a2a_discovery_cache import \
            SQLiteA2ADiscoveryCache

        ttl = self.config.a2a.discovery_cache_ttl_seconds
        self._a2a_discovery_cache = SQLiteA2ADiscoveryCache()
        self._a2a_adapter = A2AClientAdapter(
            discovery_cache=self._a2a_discovery_cache,
            discovery_cache_ttl_seconds=ttl,
        )
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

    # ── MCP resource/prompt facade ──────────────────────────────

    async def list_mcp_resources(self, server_id: str) -> list:
        if not self._mcp_manager:
            return []
        return await self._mcp_manager.list_resources(server_id)

    async def read_mcp_resource(self, server_id: str, uri: str) -> list:
        if not self._mcp_manager:
            raise RuntimeError("MCP not configured")
        return await self._mcp_manager.read_resource(server_id, uri)

    async def list_mcp_prompts(self, server_id: str) -> list:
        if not self._mcp_manager:
            return []
        return await self._mcp_manager.list_prompts(server_id)

    async def get_mcp_prompt(self, server_id: str, name: str, arguments: dict | None = None) -> dict:
        if not self._mcp_manager:
            raise RuntimeError("MCP not configured")
        return await self._mcp_manager.get_prompt(server_id, name, arguments)

    async def list_mcp_resource_templates(self, server_id: str) -> list:
        if not self._mcp_manager:
            return []
        return await self._mcp_manager.list_resource_templates(server_id)

    # ── Admin Plane API ────────────────────────────────────────
    #
    # These methods are ADMIN-PLANE interfaces for the host application.
    # They bypass ToolExecutor intentionally — they are NOT part of the
    # Agent capability plane.
    #
    # Agent/LLM capability MUST go through ToolExecutor.execute() which
    # enforces: capability policy, confirmation, error envelope, audit.
    #
    # Do NOT add new Agent-facing capabilities here. Register them as
    # @tool functions in tools/builtin/ instead.
    # ──────────────────────────────────────────────────────────

    def list_memories(self, user_id: str | None = None) -> list:
        """[Admin] List saved memories for the current agent."""
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
        user_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        content_parts: list[ContentPart] | None = None,
    ) -> AgentRunResult:
        """Run the agent on a task.

        Args:
            task: The user input / task description (text portion).
            initial_session_messages: Prior conversation history. Injected into
                the session so the model sees multi-turn context. The ContextBuilder
                automatically trims older messages when the token budget is tight.
            user_id: Optional end-user identity for memory namespace isolation.
            cancel_event: External cancellation signal. When set, the coordinator
                stops at the next iteration boundary with USER_CANCEL.
            content_parts: Multimodal content parts (images, audio, files).
                When provided, the user message carries both text content and
                multimodal parts. Adapters convert to provider-specific formats.
        """
        if not self._setup_done:
            self.setup()
        return await self._coordinator.run(
            self._agent,
            self._deps,
            task,
            initial_session_messages=initial_session_messages,
            user_id=user_id,
            cancel_event=cancel_event,
            content_parts=content_parts,
        )

    async def run_stream(
        self,
        task: str,
        initial_session_messages: list[Message] | None = None,
        user_id: str | None = None,
        cancel_event: asyncio.Event | None = None,
        content_parts: list[ContentPart] | None = None,
    ):
        """Stream agent execution, yielding StreamEvents in real-time.

        Usage:
            async for event in framework.run_stream("Hello"):
                if event.type == StreamEventType.TOKEN:
                    print(event.data["text"], end="", flush=True)
                elif event.type == StreamEventType.DONE:
                    result = event.data["result"]
        """
        if not self._setup_done:
            self.setup()
        async for event in self._coordinator.run_stream(
            self._agent,
            self._deps,
            task,
            initial_session_messages=initial_session_messages,
            user_id=user_id,
            cancel_event=cancel_event,
            content_parts=content_parts,
        ):
            yield event

    def begin_conversation(self, conversation_id: str = "") -> None:
        """Start a conversation-level stateful session.

        When active, the adapter session persists across multiple run() calls.
        First run sends full context (system prompt + user input).
        Subsequent runs send only new messages (delta).

        Only effective when config.model.session_mode = "stateful".
        """
        if not self._setup_done:
            self.setup()
        if self._deps and self._deps.model_adapter:
            adapter = self._deps.model_adapter
            if hasattr(adapter, "supports_stateful_session") and adapter.supports_stateful_session():
                adapter.begin_session(session_id=conversation_id or "conv")
                logger.info("conversation.session_started", conversation_id=conversation_id)

    def end_conversation(self) -> None:
        """End the conversation-level stateful session."""
        if self._deps and self._deps.model_adapter:
            adapter = self._deps.model_adapter
            if hasattr(adapter, "_session") and adapter._session.active:
                adapter.end_session()
                logger.info("conversation.session_ended")

    def build_a2a_server(
        self,
        *,
        name: str = "aegis-agent",
        description: str = "Aegis Agent Framework A2A Server",
        host: str = "0.0.0.0",
        port: int = 8080,
        skills: list[dict] | None = None,
    ) -> Any:
        """Build a FastAPI app exposing this framework as an A2A server.

        Usage::

            app = framework.build_a2a_server(name="my-agent", port=9000)
            uvicorn.run(app, host="0.0.0.0", port=9000)
        """
        from agent_framework.protocols.a2a.a2a_client_adapter import \
            A2AClientAdapter
        adapter = A2AClientAdapter()
        return adapter.build_a2a_server_app(
            self,
            name=name,
            description=description,
            url=f"http://{host}:{port}",
            skills=skills,
        )

    # ------------------------------------------------------------------
    # Hooks & Plugins public API
    # ------------------------------------------------------------------

    def register_hook(self, hook: Any) -> None:
        """Register a hook into this framework's hook registry."""
        self._hook_registry.register(hook)

    def unregister_hook(self, hook_id: str) -> None:
        """Remove a hook by ID."""
        self._hook_registry.unregister(hook_id)

    def list_hooks(self, hook_point: Any = None) -> list:
        """List registered hook metadata."""
        return self._hook_registry.list_hooks(hook_point=hook_point)

    def load_plugin(self, plugin: Any) -> Any:
        """Load and register a plugin."""
        from agent_framework.plugins.lifecycle import PluginLifecycleManager
        from agent_framework.plugins.loader import PluginLoader
        from agent_framework.plugins.registry import PluginRegistry
        if not hasattr(self, "_plugin_registry"):
            self._plugin_registry = PluginRegistry()
            self._plugin_lifecycle = PluginLifecycleManager(
                self._plugin_registry, self._hook_registry,
                tool_registry=self._registry,
                skill_router=getattr(self._deps, "skill_router", None) if self._deps else None,
            )
        loader = PluginLoader(self._plugin_registry)
        manifest = loader.load_plugin(plugin)
        return manifest

    def enable_plugin(self, plugin_id: str) -> None:
        """Enable a loaded plugin (registers its hooks and tools)."""
        if hasattr(self, "_plugin_lifecycle"):
            self._plugin_lifecycle.enable(plugin_id)

    def disable_plugin(self, plugin_id: str) -> None:
        """Disable an enabled plugin."""
        if hasattr(self, "_plugin_lifecycle"):
            self._plugin_lifecycle.disable(plugin_id)

    def list_plugin_agent_templates(self) -> dict[str, list]:
        """Return {plugin_id: [agent_templates]} for all enabled plugins."""
        if hasattr(self, "_plugin_lifecycle"):
            return self._plugin_lifecycle.list_agent_templates()
        return {}

    def get_plugin_agent_templates(self, plugin_id: str) -> list:
        """Return agent templates from a specific plugin."""
        if hasattr(self, "_plugin_lifecycle"):
            return self._plugin_lifecycle.get_agent_templates(plugin_id)
        return []

    async def shutdown(self) -> None:
        """Clean up resources."""
        self.end_conversation()
        if self._mcp_manager:
            await self._mcp_manager.disconnect_all()
        if self._memory_store:
            self._memory_store.close()
        if getattr(self, "_a2a_discovery_cache", None) is not None:
            self._a2a_discovery_cache.close()
        from agent_framework.infra.telemetry import get_tracing_manager
        get_tracing_manager().shutdown()
        logger.info("framework.shutdown")


def _create_memory_store(config: Any) -> Any:
    """Factory: create memory store based on config.store_type."""
    store_type = getattr(config, "store_type", "sqlite")

    if store_type == "postgresql":
        from agent_framework.memory.pg_store import PostgreSQLMemoryStore
        if not config.connection_url:
            raise ValueError("postgresql store requires memory.connection_url")
        return PostgreSQLMemoryStore(connection_url=config.connection_url)

    if store_type == "mongodb":
        from agent_framework.memory.mongo_store import MongoDBMemoryStore
        if not config.connection_url:
            raise ValueError("mongodb store requires memory.connection_url")
        db_name = config.database_name or "agent_memory"
        return MongoDBMemoryStore(connection_url=config.connection_url, database_name=db_name)

    if store_type == "neo4j":
        from agent_framework.memory.neo4j_store import Neo4jMemoryStore
        if not config.connection_url:
            raise ValueError("neo4j store requires memory.connection_url")
        auth = ("neo4j", "neo4j")
        if hasattr(config, "neo4j_auth") and config.neo4j_auth:
            parts = config.neo4j_auth.split(":", 1)
            auth = (parts[0], parts[1] if len(parts) > 1 else "")
        db_name = config.database_name or "neo4j"
        return Neo4jMemoryStore(connection_url=config.connection_url, auth=auth, database=db_name)

    # Default: sqlite
    return SQLiteMemoryStore(db_path=config.db_path)

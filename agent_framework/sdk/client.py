"""AgentSDK — clean public API for embedding the agent framework.

This is the ONLY class external consumers should interact with.
All internal wiring is hidden behind this facade.

Full capability coverage:
  - Agent execution: run / run_stream / run_sync
  - Conversation: multi-turn stateful sessions
  - Tool management: register / list / remove
  - Skill management: register / list / remove / activate
  - Memory management: list / forget / pin / unpin / clear / enable/disable
  - Hook management: register / unregister / list
  - Plugin management: load / enable / disable / list
  - MCP integration: connect / list resources / read resources
  - A2A integration: discover / build server
  - Model catalog: list / resolve
  - JSONL streaming: pipe-friendly structured output
  - Team notifications: drain / peek / mark delivered
  - Agent identity: resolve identity metadata
  - Workspace: init / list templates
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, Callable

from agent_framework.infra.logger import get_logger
from agent_framework.sdk.config import SDKConfig
from agent_framework.sdk.types import (
    SDKAgentInfo,
    SDKHookInfo,
    SDKMCPServerInfo,
    SDKMemoryEntry,
    SDKModelInfo,
    SDKPluginInfo,
    SDKRunResult,
    SDKSkillInfo,
    SDKStreamEvent,
    SDKStreamEventType,
    SDKTeamNotification,
    SDKToolDefinition,
    SDKToolInfo,
)

logger = get_logger(__name__)


class AgentSDK:
    """Public SDK facade for the agent framework.

    Usage:
        sdk = AgentSDK(SDKConfig(model_adapter_type="anthropic", api_key="sk-..."))
        result = await sdk.run("Hello!")
        print(result.final_answer)

    All capabilities of the agent framework are accessible through this
    single class. Internal framework types never leak to consumers.
    """

    def __init__(self, config: SDKConfig | None = None) -> None:
        self._config = config or SDKConfig()
        self._framework: Any = None
        self._setup_done = False
        self._custom_tools: list[tuple[Callable, SDKToolDefinition]] = []

    @property
    def config(self) -> SDKConfig:
        """Current SDK configuration (read-only)."""
        return self._config

    @property
    def is_setup(self) -> bool:
        """Whether the framework has been initialized."""
        return self._setup_done

    def setup(self) -> None:
        """Initialize the framework. Called automatically on first use."""
        from agent_framework.entry import AgentFramework
        from agent_framework.infra.config import FrameworkConfig

        fw_config = FrameworkConfig(**self._config.to_framework_config())
        self._framework = AgentFramework(config=fw_config)
        self._framework.setup(auto_approve_tools=self._config.auto_approve_tools)

        # Register custom tools queued before setup
        for callable_ref, tool_def in self._custom_tools:
            self._framework.register_tool(
                callable_ref,
                name=tool_def.name,
                description=tool_def.description,
                category=tool_def.category,
            )

        self._setup_done = True

    def _ensure_setup(self) -> None:
        if not self._setup_done:
            self.setup()

    # ==================================================================
    # Tool Registration
    # ==================================================================

    def tool(
        self,
        name: str,
        description: str = "",
        category: str = "custom",
        require_confirm: bool = False,
    ) -> Callable:
        """Decorator to register a custom tool.

        Usage:
            @sdk.tool(name="my_tool", description="Does something")
            def my_tool(arg: str) -> str:
                return f"Result: {arg}"
        """
        tool_def = SDKToolDefinition(
            name=name,
            description=description,
            category=category,
            require_confirm=require_confirm,
        )

        def decorator(func: Callable) -> Callable:
            self._custom_tools.append((func, tool_def))
            if self._setup_done and self._framework is not None:
                self._framework.register_tool(
                    func, name=name, description=description, category=category,
                )
            return func

        return decorator

    def register_tool(self, func: Callable) -> None:
        """Register a @tool-decorated function directly."""
        self._ensure_setup()
        self._framework.register_tool(func)

    def register_tools_from_module(self, module: Any) -> int:
        """Register all @tool functions from a Python module."""
        self._ensure_setup()
        return self._framework.register_tools_from_module(module)

    def list_tools(self, category: str | None = None) -> list[SDKToolInfo]:
        """List all registered tools."""
        self._ensure_setup()
        if self._framework._registry is None:
            return []
        tools = self._framework._registry.list_tools(category=category)
        return [
            SDKToolInfo(
                name=t.meta.name,
                description=t.meta.description,
                category=t.meta.category,
                source=t.meta.source,
                require_confirm=t.meta.require_confirm,
                is_async=t.meta.is_async,
                tags=list(t.meta.tags),
            )
            for t in tools
        ]

    # ==================================================================
    # Agent Execution
    # ==================================================================

    async def run(
        self,
        task: str,
        *,
        user_id: str | None = None,
        timeout_ms: int | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        content_parts: list[dict[str, Any]] | None = None,
    ) -> SDKRunResult:
        """Run the agent on a task and return the result.

        Args:
            task: The task or question text.
            user_id: User identity for memory namespace isolation.
            timeout_ms: Run timeout in milliseconds.
            conversation_history: Prior conversation as list of
                {"role": "user"|"assistant", "content": "..."} dicts.
            content_parts: Multimodal content (images, files).
        """
        self._ensure_setup()

        # Convert conversation history to internal Message objects
        initial_messages = None
        if conversation_history:
            from agent_framework.models.message import Message
            initial_messages = [
                Message(role=m["role"], content=m["content"])
                for m in conversation_history
            ]

        # Convert content parts to internal ContentPart objects
        parts = None
        if content_parts:
            from agent_framework.models.message import ContentPart
            parts = [ContentPart(**p) for p in content_parts]

        result = await self._framework.run(
            task,
            initial_session_messages=initial_messages,
            user_id=user_id,
            content_parts=parts,
        )

        return self._convert_run_result(result)

    def run_sync(
        self,
        task: str,
        *,
        user_id: str | None = None,
        timeout_ms: int | None = None,
    ) -> SDKRunResult:
        """Synchronous wrapper for run(). Convenience for non-async code."""
        return asyncio.run(self.run(task, user_id=user_id, timeout_ms=timeout_ms))

    async def run_stream(
        self,
        task: str,
        *,
        user_id: str | None = None,
        timeout_ms: int | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        content_parts: list[dict[str, Any]] | None = None,
    ) -> AsyncGenerator[SDKStreamEvent, None]:
        """Stream agent execution events in real-time.

        Yields SDKStreamEvent objects. The last event is always DONE or ERROR.
        """
        self._ensure_setup()

        initial_messages = None
        if conversation_history:
            from agent_framework.models.message import Message
            initial_messages = [
                Message(role=m["role"], content=m["content"])
                for m in conversation_history
            ]

        parts = None
        if content_parts:
            from agent_framework.models.message import ContentPart
            parts = [ContentPart(**p) for p in content_parts]

        async for event in self._framework.run_stream(
            task,
            initial_session_messages=initial_messages,
            user_id=user_id,
            content_parts=parts,
        ):
            yield self._convert_stream_event(event)

    async def run_stream_jsonl(
        self,
        task: str,
        *,
        user_id: str | None = None,
    ) -> AsyncGenerator[str, None]:
        """Stream agent execution as JSONL lines (pipe-friendly).

        Each yielded string is a single JSON line (no trailing newline).
        Suitable for CI/CD pipelines, log aggregation, and IPC.
        """
        async for event in self.run_stream(task, user_id=user_id):
            from agent_framework.models.stream import StreamEvent, StreamEventType
            internal = StreamEvent(
                type=StreamEventType(event.type.value)
                if event.type.value in StreamEventType.__members__.values()
                else StreamEventType.TOKEN,
                data=event.data,
            )
            yield internal.to_jsonl()

    # ==================================================================
    # Conversation Management
    # ==================================================================

    def begin_conversation(self, conversation_id: str = "") -> None:
        """Begin a multi-turn stateful conversation session.

        When active, the model adapter session persists across run() calls.
        First run sends full context; subsequent runs send only deltas.
        Only effective when session_mode="stateful".
        """
        self._ensure_setup()
        self._framework.begin_conversation(conversation_id)

    def end_conversation(self) -> None:
        """End the current conversation session."""
        if self._framework:
            self._framework.end_conversation()

    # ==================================================================
    # Memory Management
    # ==================================================================

    def list_memories(self, user_id: str | None = None) -> list[SDKMemoryEntry]:
        """List stored memories."""
        self._ensure_setup()
        raw = self._framework.list_memories(user_id=user_id)
        return [
            SDKMemoryEntry(
                memory_id=str(m.get("memory_id", m.get("id", ""))),
                content=m.get("content", ""),
                kind=m.get("kind", ""),
                tags=m.get("tags", []),
                pinned=m.get("pinned", False),
                active=m.get("active", True),
            )
            for m in (raw or [])
        ]

    def forget_memory(self, memory_id: str) -> None:
        """Delete a memory by ID."""
        self._ensure_setup()
        self._framework.forget_memory(memory_id)

    def pin_memory(self, memory_id: str) -> None:
        """Pin a memory (always included in context)."""
        self._ensure_setup()
        self._framework.pin_memory(memory_id)

    def unpin_memory(self, memory_id: str) -> None:
        """Unpin a memory."""
        self._ensure_setup()
        self._framework.unpin_memory(memory_id)

    def activate_memory(self, memory_id: str) -> None:
        """Activate a deactivated memory."""
        self._ensure_setup()
        self._framework.activate_memory(memory_id)

    def deactivate_memory(self, memory_id: str) -> None:
        """Deactivate a memory (excluded from context but not deleted)."""
        self._ensure_setup()
        self._framework.deactivate_memory(memory_id)

    def clear_memories(self, user_id: str | None = None) -> int:
        """Clear all memories. Returns count of deleted entries."""
        self._ensure_setup()
        return self._framework.clear_memories(user_id=user_id)

    def set_memory_enabled(self, enabled: bool) -> None:
        """Enable or disable the memory system."""
        self._ensure_setup()
        self._framework.set_memory_enabled(enabled)

    # ==================================================================
    # Skill Management
    # ==================================================================

    def register_skill(
        self,
        skill_id: str,
        name: str,
        description: str = "",
        trigger_keywords: list[str] | None = None,
        system_prompt_addon: str = "",
        model_override: str | None = None,
    ) -> None:
        """Register a skill for keyword-based activation."""
        self._ensure_setup()
        from agent_framework.models.agent import Skill
        skill = Skill(
            skill_id=skill_id,
            name=name,
            description=description,
            trigger_keywords=trigger_keywords or [],
            system_prompt_addon=system_prompt_addon,
            model_override=model_override,
        )
        self._framework.register_skill(skill)

    def list_skills(self) -> list[SDKSkillInfo]:
        """List all registered skills."""
        self._ensure_setup()
        skills = self._framework.list_skills()
        return [
            SDKSkillInfo(
                skill_id=s.skill_id,
                name=s.name,
                description=s.description,
                trigger_keywords=s.trigger_keywords,
                user_invocable=s.user_invocable,
                source_path=s.source_path,
            )
            for s in skills
        ]

    def remove_skill(self, skill_id: str) -> bool:
        """Remove a skill by ID. Returns True if found."""
        self._ensure_setup()
        return self._framework.remove_skill(skill_id)

    def activate_skill(self, skill_id: str) -> None:
        """Activate a skill in interactive mode."""
        self._ensure_setup()
        skills = self._framework.list_skills()
        for s in skills:
            if s.skill_id == skill_id:
                self._framework.activate_skill(s)
                return
        raise ValueError(f"Skill not found: {skill_id}")

    def deactivate_skill(self) -> None:
        """Deactivate the current interactive skill."""
        self._ensure_setup()
        self._framework.deactivate_skill()

    def get_active_skill(self) -> SDKSkillInfo | None:
        """Get the currently active skill, if any."""
        self._ensure_setup()
        s = self._framework.get_active_skill()
        if s is None:
            return None
        return SDKSkillInfo(
            skill_id=s.skill_id,
            name=s.name,
            description=s.description,
        )

    # ==================================================================
    # Hook Management
    # ==================================================================

    def register_hook(self, hook: Any) -> None:
        """Register a hook into the framework's hook registry."""
        self._ensure_setup()
        self._framework.register_hook(hook)

    def unregister_hook(self, hook_id: str) -> None:
        """Remove a hook by ID."""
        self._ensure_setup()
        self._framework.unregister_hook(hook_id)

    def list_hooks(self, hook_point: str | None = None) -> list[SDKHookInfo]:
        """List registered hooks."""
        self._ensure_setup()
        raw = self._framework.list_hooks(hook_point=hook_point)
        return [
            SDKHookInfo(
                hook_id=h.get("hook_id", h.get("id", "")),
                hook_point=h.get("hook_point", ""),
                description=h.get("description", ""),
                priority=h.get("priority", 0),
            )
            for h in (raw or [])
            if isinstance(h, dict)
        ]

    # ==================================================================
    # Plugin Management
    # ==================================================================

    def load_plugin(self, plugin: Any) -> Any:
        """Load and register a plugin. Returns plugin manifest."""
        self._ensure_setup()
        return self._framework.load_plugin(plugin)

    def enable_plugin(self, plugin_id: str) -> None:
        """Enable a loaded plugin."""
        self._ensure_setup()
        self._framework.enable_plugin(plugin_id)

    def disable_plugin(self, plugin_id: str) -> None:
        """Disable an enabled plugin."""
        self._ensure_setup()
        self._framework.disable_plugin(plugin_id)

    def list_plugins(self) -> list[SDKPluginInfo]:
        """List all plugins and their status."""
        self._ensure_setup()
        registry = getattr(self._framework, "_plugin_registry_obj", None)
        if registry is None:
            return []
        plugins = registry.list_plugins()
        enabled = {p.plugin_id for p in registry.list_enabled()}
        return [
            SDKPluginInfo(
                plugin_id=p.plugin_id,
                name=p.name,
                version=p.version,
                description=p.description,
                enabled=p.plugin_id in enabled,
                state=getattr(p, "state", ""),
            )
            for p in plugins
        ]

    # ==================================================================
    # MCP Integration
    # ==================================================================

    async def setup_mcp(self) -> None:
        """Connect to configured MCP servers and sync their tools."""
        self._ensure_setup()
        await self._framework.setup_mcp()

    async def list_mcp_resources(self, server_id: str) -> list[dict[str, Any]]:
        """List resources from an MCP server."""
        self._ensure_setup()
        return await self._framework.list_mcp_resources(server_id)

    async def read_mcp_resource(self, server_id: str, uri: str) -> list[Any]:
        """Read a resource from an MCP server."""
        self._ensure_setup()
        return await self._framework.read_mcp_resource(server_id, uri)

    async def list_mcp_prompts(self, server_id: str) -> list[dict[str, Any]]:
        """List prompts from an MCP server."""
        self._ensure_setup()
        return await self._framework.list_mcp_prompts(server_id)

    async def get_mcp_prompt(
        self, server_id: str, name: str, arguments: dict | None = None
    ) -> dict[str, Any]:
        """Get a prompt from an MCP server."""
        self._ensure_setup()
        return await self._framework.get_mcp_prompt(server_id, name, arguments)

    # ==================================================================
    # A2A Integration
    # ==================================================================

    async def setup_a2a(self) -> None:
        """Discover configured A2A agents and register their tools."""
        self._ensure_setup()
        await self._framework.setup_a2a()

    def build_a2a_server(
        self,
        *,
        name: str = "agent-sdk-server",
        description: str = "Agent SDK A2A Server",
        host: str = "0.0.0.0",
        port: int = 8080,
        skills: list[dict] | None = None,
    ) -> Any:
        """Build a FastAPI app exposing this SDK as an A2A server.

        Returns a FastAPI app suitable for uvicorn.run().
        """
        self._ensure_setup()
        return self._framework.build_a2a_server(
            name=name, description=description,
            host=host, port=port, skills=skills,
        )

    # ==================================================================
    # Model Catalog
    # ==================================================================

    def list_models(self, provider: str | None = None) -> list[SDKModelInfo]:
        """List available models from the catalog."""
        self._ensure_setup()
        raw = self._framework.list_available_models(provider)
        return [
            SDKModelInfo(
                model_id=m.get("model_id", ""),
                provider=m.get("provider", ""),
                display_name=m.get("display_name", ""),
                context_window=m.get("context_window", 0),
                supports_vision=m.get("supports_vision", False),
                supports_tools=m.get("supports_tools", False),
            )
            for m in (raw or [])
            if isinstance(m, dict)
        ]

    def resolve_model_id(self, raw_id: str) -> str:
        """Normalize a model ID to its canonical form."""
        self._ensure_setup()
        return self._framework.resolve_model_id(raw_id)

    # ==================================================================
    # Team Notifications
    # ==================================================================

    def drain_team_notifications(self) -> list[SDKTeamNotification]:
        """Drain pending team notifications."""
        self._ensure_setup()
        raw = self._framework.drain_team_notifications()
        return [
            SDKTeamNotification(
                role=n.get("role", ""),
                status=n.get("status", ""),
                summary=n.get("summary", ""),
                task=n.get("task", ""),
                agent_id=n.get("agent_id", ""),
                notification_type=n.get("notification_type", ""),
            )
            for n in (raw or [])
        ]

    def has_pending_team_notifications(self) -> bool:
        """Check for pending team notifications."""
        self._ensure_setup()
        return self._framework.has_pending_team_notifications()

    def mark_team_notifications_delivered(
        self, agent_ids: list[str] | None = None
    ) -> None:
        """Mark team notifications as delivered."""
        self._ensure_setup()
        self._framework.mark_team_notifications_delivered(agent_ids)

    def drain_team_summaries(self) -> list[str]:
        """Drain auto-generated team summaries."""
        self._ensure_setup()
        return self._framework.drain_team_summaries()

    # ==================================================================
    # Agent Identity
    # ==================================================================

    def resolve_identity(self, agent_id: str | None = None) -> dict[str, Any]:
        """Resolve identity metadata for an agent."""
        self._ensure_setup()
        identity = self._framework.resolve_agent_identity(agent_id)
        if hasattr(identity, "model_dump"):
            return identity.model_dump()
        return {"agent_id": agent_id or "default"}

    # ==================================================================
    # Workspace
    # ==================================================================

    @staticmethod
    def init_workspace(template: str = "default", target_dir: str = ".") -> list[str]:
        """Initialize workspace from template. Returns created file paths."""
        from agent_framework.entry import AgentFramework
        return AgentFramework.init_workspace(template=template, target_dir=target_dir)

    @staticmethod
    def list_workspace_templates() -> list[str]:
        """List available workspace templates."""
        from agent_framework.entry import AgentFramework
        return AgentFramework.list_workspace_templates()

    # ==================================================================
    # Agent Info
    # ==================================================================

    def get_info(self) -> SDKAgentInfo:
        """Get comprehensive agent runtime information."""
        self._ensure_setup()
        fw = self._framework

        tools = []
        tool_count = 0
        if fw._registry:
            tool_entries = fw._registry.list_tools()
            tools = [t.meta.name for t in tool_entries]
            tool_count = len(tool_entries)

        skills = []
        skills_count = 0
        if fw._deps and hasattr(fw._deps, "skill_router"):
            skill_list = fw._deps.skill_router.list_skills()
            skills = [s.name for s in skill_list]
            skills_count = len(skill_list)

        plugins_count = 0
        if hasattr(fw, "_plugin_registry_obj") and fw._plugin_registry_obj:
            plugins_count = len(fw._plugin_registry_obj.list_enabled())

        hooks_count = 0
        if hasattr(fw, "_hook_registry") and fw._hook_registry:
            hooks_count = len(fw._hook_registry.list_hooks())

        return SDKAgentInfo(
            agent_id=fw._agent.agent_id if fw._agent else "default",
            model_name=self._config.model_name,
            adapter_type=self._config.model_adapter_type,
            approval_mode=self._config.approval_mode,
            max_iterations=self._config.max_iterations,
            shell_enabled=self._config.shell_enabled,
            sandbox_enabled=self._config.sandbox_enabled,
            memory_enabled=self._config.memory_enabled,
            spawn_enabled=self._config.allow_spawn,
            tools_count=tool_count,
            skills_count=skills_count,
            plugins_count=plugins_count,
            hooks_count=hooks_count,
            tools_available=tools,
            skills_available=skills,
        )

    # ==================================================================
    # Cleanup
    # ==================================================================

    async def shutdown(self) -> None:
        """Release all framework resources (MCP connections, memory stores, etc.)."""
        if self._framework:
            await self._framework.shutdown()

    async def cleanup(self) -> None:
        """Alias for shutdown()."""
        await self.shutdown()

    # ==================================================================
    # Context Manager
    # ==================================================================

    async def __aenter__(self) -> AgentSDK:
        self._ensure_setup()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.shutdown()

    # ==================================================================
    # Internal Helpers
    # ==================================================================

    @staticmethod
    def _convert_run_result(result: Any) -> SDKRunResult:
        """Convert internal AgentRunResult to SDK-public SDKRunResult."""
        return SDKRunResult(
            success=result.success,
            final_answer=result.final_answer,
            error=result.error,
            iterations_used=result.iterations_used,
            total_tokens=result.usage.total_tokens if result.usage else 0,
            run_id=result.run_id,
            stop_reason=result.stop_signal.reason.value if result.stop_signal else "",
            termination_kind=result.termination_kind.value if hasattr(result, "termination_kind") else "",
            artifacts=[
                {"name": a.name, "type": a.artifact_type, "uri": a.uri}
                for a in (result.artifacts or [])
            ],
            progressive_responses=list(result.progressive_responses or []),
        )

    @staticmethod
    def _convert_stream_event(event: Any) -> SDKStreamEvent:
        """Convert internal StreamEvent to SDK-public SDKStreamEvent."""
        import time

        type_map = {
            "token": SDKStreamEventType.TOKEN,
            "tool_call_start": SDKStreamEventType.TOOL_START,
            "tool_call_done": SDKStreamEventType.TOOL_DONE,
            "progressive_start": SDKStreamEventType.TOOL_START,
            "progressive_done": SDKStreamEventType.TOOL_DONE,
            "thinking_start": SDKStreamEventType.THINKING,
            "thinking_delta": SDKStreamEventType.THINKING,
            "thinking_end": SDKStreamEventType.THINKING,
            "iteration_start": SDKStreamEventType.ITERATION_START,
            "subagent_stream": SDKStreamEventType.SUBAGENT_EVENT,
            "done": SDKStreamEventType.DONE,
            "error": SDKStreamEventType.ERROR,
        }

        event_type_str = event.type.value if hasattr(event.type, "value") else str(event.type)
        sdk_type = type_map.get(event_type_str, SDKStreamEventType.TOKEN)

        return SDKStreamEvent(
            type=sdk_type,
            data=dict(event.data) if event.data else {},
            timestamp_ms=int(time.time() * 1000),
        )

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
  - Slash commands: execute framework commands programmatically
  - Cancellation: cancel running tasks via SDKCancelToken
  - Event callbacks: subscribe to SDK lifecycle events
  - Context stats: inspect context engineering statistics
  - Manual compression: trigger history compaction
  - Approval mode: switch approval mode at runtime
  - Checkpoints: save / list / restore state snapshots
  - Policy rules: add / list / clear declarative policy rules
  - Fork: create independent child SDK instances
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Callable

from agent_framework.infra.logger import get_logger
from agent_framework.sdk.config import SDKConfig
from agent_framework.sdk.types import (
    SDKAgentInfo,
    SDKCancelToken,
    SDKCheckpoint,
    SDKCommandResult,
    SDKContextStats,
    SDKEventSubscription,
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

# Valid event types for the event callback system
_VALID_EVENT_TYPES = frozenset({
    "tool_start",
    "tool_done",
    "iteration_start",
    "run_start",
    "run_done",
    "error",
})


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
        self._event_callbacks: dict[str, tuple[str, Callable]] = {}
        self._policy_rules: list[dict[str, Any]] = []

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
        cancel_token: SDKCancelToken | None = None,
    ) -> SDKRunResult:
        """Run the agent on a task and return the result.

        Args:
            task: The task or question text.
            user_id: User identity for memory namespace isolation.
            timeout_ms: Run timeout in milliseconds.
            conversation_history: Prior conversation as list of
                {"role": "user"|"assistant", "content": "..."} dicts.
            content_parts: Multimodal content (images, files).
            cancel_token: Optional cancellation token. Call
                cancel_token.cancel() to stop the run at the next
                iteration boundary.
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

        # Extract asyncio.Event from cancel token if provided
        cancel_event = cancel_token.event if cancel_token is not None else None

        self._fire_event("run_start", {"task": task})

        try:
            result = await self._framework.run(
                task,
                initial_session_messages=initial_messages,
                user_id=user_id,
                cancel_event=cancel_event,
                content_parts=parts,
            )
            sdk_result = self._convert_run_result(result)
            self._fire_event("run_done", {"run_id": sdk_result.run_id, "success": sdk_result.success})
            return sdk_result
        except Exception as exc:
            self._fire_event("error", {"error": str(exc), "phase": "run"})
            raise

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
        cancel_token: SDKCancelToken | None = None,
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

        cancel_event = cancel_token.event if cancel_token is not None else None

        self._fire_event("run_start", {"task": task})

        async for event in self._framework.run_stream(
            task,
            initial_session_messages=initial_messages,
            user_id=user_id,
            cancel_event=cancel_event,
            content_parts=parts,
        ):
            sdk_event = self._convert_stream_event(event)

            # Fire matching event callbacks during stream processing
            event_type_str = sdk_event.type.value
            if event_type_str in _VALID_EVENT_TYPES:
                self._fire_event(event_type_str, sdk_event.data)

            yield sdk_event

        self._fire_event("run_done", {"task": task})

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
    # Slash Command Execution
    # ==================================================================

    async def execute_command(self, command: str) -> dict[str, Any]:
        """Execute a slash command (e.g. '/init', '/memory show', '/model list').

        Returns a dict with 'type' and command-specific data fields:
          - type='message': content, message_type
          - type='tool': tool_name, tool_args, result
          - type='submit_prompt': prompt, result
          - type='load_history': message_count
          - type='none': (command returned None)
        """
        self._ensure_setup()

        from agent_framework.commands.protocol import (
            CommandContext,
            LoadHistoryAction,
            MessageAction,
            SubmitPromptAction,
            ToolAction,
        )
        from agent_framework.commands.registry import CommandRegistry

        # Parse command string: "/name args..." or "name args..."
        raw = command.strip()
        if raw.startswith("/"):
            raw = raw[1:]

        parts = raw.split(maxsplit=1)
        cmd_name = parts[0] if parts else ""
        cmd_args = parts[1] if len(parts) > 1 else ""

        if not cmd_name:
            return {"type": "error", "content": "Empty command"}

        # Build context from framework internals
        context = CommandContext(
            framework=self._framework,
            config=getattr(self._framework, "_config", None),
            state=None,
            args=cmd_args,
        )

        # Use framework's command registry if available, otherwise create one
        registry: CommandRegistry | None = getattr(
            self._framework, "_command_registry", None
        )
        if registry is None:
            registry = CommandRegistry()

        action = await registry.dispatch(cmd_name, context, cmd_args)

        if action is None:
            return {"type": "none"}

        # Convert action to SDK-friendly dict
        if isinstance(action, MessageAction):
            return {
                "type": "message",
                "message_type": action.message_type,
                "content": action.content,
            }

        if isinstance(action, ToolAction):
            # Execute the tool through the framework
            tool_result: Any = None
            try:
                executor = self._framework._deps.tool_executor
                tool_result = await executor.execute(
                    action.tool_name, action.tool_args
                )
            except Exception as exc:
                tool_result = f"Tool execution failed: {exc}"

            return {
                "type": "tool",
                "tool_name": action.tool_name,
                "tool_args": action.tool_args,
                "result": tool_result,
            }

        if isinstance(action, SubmitPromptAction):
            # Run the agent with the injected prompt
            result = await self.run(action.content)
            return {
                "type": "submit_prompt",
                "prompt": action.content,
                "result": result.model_dump(),
            }

        if isinstance(action, LoadHistoryAction):
            return {
                "type": "load_history",
                "message_count": len(action.messages),
            }

        # Fallback for unknown action types
        if hasattr(action, "model_dump"):
            return {"type": getattr(action, "type", "unknown"), **action.model_dump()}

        return {"type": "unknown", "raw": str(action)}

    # ==================================================================
    # Cancel Mechanism
    # ==================================================================

    @staticmethod
    def create_cancel_token() -> SDKCancelToken:
        """Create a cancellation token for a running task.

        Usage:
            token = sdk.create_cancel_token()
            # Pass to run/run_stream:
            task = asyncio.create_task(sdk.run("long task", cancel_token=token))
            # Later, from another coroutine or thread:
            token.cancel()
        """
        return SDKCancelToken()

    # ==================================================================
    # Event Callbacks
    # ==================================================================

    def on_event(self, event_type: str, callback: Callable) -> str:
        """Subscribe to SDK events. Returns a subscription ID.

        Supported event types:
          - "tool_start":       fired when a tool invocation begins
          - "tool_done":        fired when a tool invocation completes
          - "iteration_start":  fired at the start of each agent iteration
          - "run_start":        fired when a run() or run_stream() begins
          - "run_done":         fired when a run completes
          - "error":            fired on errors during execution

        The callback receives a single dict argument with event-specific data.
        """
        if event_type not in _VALID_EVENT_TYPES:
            raise ValueError(
                f"Unknown event type: {event_type!r}. "
                f"Valid types: {sorted(_VALID_EVENT_TYPES)}"
            )

        subscription_id = uuid.uuid4().hex[:12]
        self._event_callbacks[subscription_id] = (event_type, callback)
        return subscription_id

    def off_event(self, subscription_id: str) -> None:
        """Unsubscribe from events by subscription ID.

        Silently ignores unknown subscription IDs.
        """
        self._event_callbacks.pop(subscription_id, None)

    def _fire_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Invoke all registered callbacks for the given event type."""
        for _sub_id, (registered_type, callback) in list(self._event_callbacks.items()):
            if registered_type != event_type:
                continue
            try:
                callback(data)
            except Exception:
                logger.warning(
                    "event_callback_error",
                    event_type=event_type,
                    subscription_id=_sub_id,
                    exc_info=True,
                )

    # ==================================================================
    # Context Stats
    # ==================================================================

    def get_context_stats(self) -> SDKContextStats:
        """Get last context engineering statistics.

        Returns zeros if no run has been executed yet.
        """
        self._ensure_setup()

        deps = getattr(self._framework, "_deps", None)
        if deps is None:
            return SDKContextStats()

        context_engineer = getattr(deps, "context_engineer", None)
        if context_engineer is None:
            return SDKContextStats()

        try:
            stats = context_engineer.report_context_stats()
        except Exception:
            return SDKContextStats()

        return SDKContextStats(
            system_tokens=getattr(stats, "system_tokens", 0),
            memory_tokens=getattr(stats, "memory_tokens", 0),
            session_tokens=getattr(stats, "session_tokens", 0),
            total_tokens=getattr(stats, "total_tokens", 0),
            groups_trimmed=getattr(stats, "groups_trimmed", 0),
            prefix_reused=getattr(stats, "prefix_reused", False),
        )

    # ==================================================================
    # Manual Compression
    # ==================================================================

    async def compact_history(self, instruction: str = "") -> SDKRunResult:
        """Manually trigger conversation history compression.

        Submits a compression instruction to the agent framework, which
        causes the context engineer to compress the current session
        history. If no instruction is provided, a default is used.

        Returns the run result from the compression pass.
        """
        default_instruction = (
            "Please compress and summarize the conversation history so far, "
            "preserving key decisions, facts, and context."
        )
        prompt = instruction or default_instruction
        return await self.run(prompt)

    # ==================================================================
    # Approval Mode Switching
    # ==================================================================

    def set_approval_mode(self, mode: str) -> None:
        """Switch approval mode at runtime.

        Valid modes:
          - 'DEFAULT': prompt user for confirmation on destructive tools
          - 'AUTO_EDIT': auto-approve file edits, prompt for others
          - 'PLAN': plan-only mode, no tool execution

        Raises ValueError for invalid modes.
        """
        valid_modes = {"DEFAULT", "AUTO_EDIT", "PLAN"}
        if mode not in valid_modes:
            raise ValueError(
                f"Invalid approval mode: {mode!r}. Valid modes: {sorted(valid_modes)}"
            )

        self._config = self._config.model_copy(update={"approval_mode": mode})

        # Apply to running framework if setup
        if self._setup_done and self._framework is not None:
            fw_config = getattr(self._framework, "_config", None)
            if fw_config is not None and hasattr(fw_config, "tools"):
                tools_config = fw_config.tools
                if hasattr(tools_config, "approval_mode"):
                    # Update the tools config with new approval mode
                    object.__setattr__(tools_config, "approval_mode", mode)

            # Swap confirmation handler based on mode
            if mode == "DEFAULT":
                from agent_framework.tools.confirmation import (
                    CLIConfirmationHandler,
                )
                self._framework._deps.confirmation_handler = CLIConfirmationHandler()
            elif mode in ("AUTO_EDIT", "PLAN"):
                from agent_framework.tools.confirmation import (
                    AutoApproveConfirmationHandler,
                )
                self._framework._deps.confirmation_handler = AutoApproveConfirmationHandler()

    # ==================================================================
    # Checkpoint Management
    # ==================================================================

    async def save_checkpoint(self, description: str = "") -> str:
        """Save current state as a checkpoint. Returns the checkpoint_id.

        Captures conversation history and (if available) the current git
        commit hash. The checkpoint is persisted as a JSON file in the
        configured checkpoint directory.
        """
        self._ensure_setup()

        from agent_framework.commands.restore_cmd import (
            CheckpointData,
            DEFAULT_CHECKPOINT_DIR,
            save_checkpoint as _save_checkpoint,
        )

        checkpoint_id = uuid.uuid4().hex[:12]

        # Capture git commit hash if in a git repository
        git_hash: str | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "rev-parse", "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                git_hash = stdout.decode().strip()
        except (FileNotFoundError, OSError):
            pass

        # Capture conversation messages from session state
        conversation_messages: list[dict[str, Any]] = []
        session_state = getattr(self._framework, "_last_session_state", None)
        if session_state is not None:
            messages = getattr(session_state, "messages", [])
            for msg in messages:
                if hasattr(msg, "model_dump"):
                    conversation_messages.append(msg.model_dump())
                elif isinstance(msg, dict):
                    conversation_messages.append(msg)

        data = CheckpointData(
            checkpoint_id=checkpoint_id,
            git_commit_hash=git_hash,
            conversation_messages=conversation_messages,
            description=description,
        )

        checkpoint_dir = self._resolve_checkpoint_dir()
        _save_checkpoint(checkpoint_dir, data)

        return checkpoint_id

    def list_checkpoints(self) -> list[dict[str, Any]]:
        """List available checkpoints.

        Returns a list of dicts with checkpoint metadata, sorted by
        creation time (newest first).
        """
        self._ensure_setup()

        from agent_framework.commands.restore_cmd import (
            list_checkpoints as _list_checkpoints,
        )

        checkpoint_dir = self._resolve_checkpoint_dir()
        checkpoints = _list_checkpoints(checkpoint_dir)

        return [
            {
                "checkpoint_id": cp.checkpoint_id,
                "created_at": cp.created_at,
                "description": cp.description,
                "git_commit_hash": cp.git_commit_hash,
                "has_conversation": bool(cp.conversation_messages),
                "has_tool_call": cp.tool_call is not None,
            }
            for cp in checkpoints
        ]

    async def restore_checkpoint(self, checkpoint_id: str) -> SDKRunResult:
        """Restore from a checkpoint.

        Finds the checkpoint matching the given ID (or prefix), performs
        git checkout if applicable, and restores conversation history.

        Returns an SDKRunResult summarizing the restore outcome.
        """
        self._ensure_setup()

        from agent_framework.commands.restore_cmd import (
            list_checkpoints as _list_checkpoints,
            perform_restore,
        )
        from agent_framework.commands.protocol import (
            LoadHistoryAction,
            MessageAction,
            ToolAction,
        )

        checkpoint_dir = self._resolve_checkpoint_dir()
        checkpoints = _list_checkpoints(checkpoint_dir)

        matches = [
            cp for cp in checkpoints
            if cp.checkpoint_id.startswith(checkpoint_id)
        ]

        if not matches:
            return SDKRunResult(
                success=False,
                error=f"No checkpoint found matching '{checkpoint_id}'.",
            )

        if len(matches) > 1:
            ids = ", ".join(m.checkpoint_id for m in matches)
            return SDKRunResult(
                success=False,
                error=(
                    f"Ambiguous checkpoint prefix '{checkpoint_id}' matches "
                    f"{len(matches)} checkpoints: {ids}. "
                    f"Please provide a longer prefix."
                ),
            )

        checkpoint = matches[0]

        # Collect all restore actions
        messages: list[str] = []
        async for action in perform_restore(checkpoint):
            if isinstance(action, MessageAction):
                if action.message_type == "error":
                    return SDKRunResult(
                        success=False,
                        error=action.content,
                    )
                messages.append(action.content)
            elif isinstance(action, LoadHistoryAction):
                messages.append(
                    f"Restored {len(action.messages)} conversation messages."
                )
            elif isinstance(action, ToolAction):
                messages.append(
                    f"Re-executing tool: {action.tool_name}"
                )
                # Execute the tool replay
                try:
                    executor = self._framework._deps.tool_executor
                    await executor.execute(action.tool_name, action.tool_args)
                except Exception as exc:
                    messages.append(f"Tool replay failed: {exc}")

        return SDKRunResult(
            success=True,
            final_answer="; ".join(messages) if messages else "Checkpoint restored.",
        )

    def _resolve_checkpoint_dir(self) -> str:
        """Determine the checkpoint directory from config or default."""
        from agent_framework.commands.restore_cmd import DEFAULT_CHECKPOINT_DIR

        fw_config = getattr(self._framework, "_config", None)
        if fw_config is not None:
            checkpoint_dir = getattr(fw_config, "checkpoint_dir", None)
            if checkpoint_dir:
                return str(checkpoint_dir)

        return DEFAULT_CHECKPOINT_DIR

    # ==================================================================
    # Policy Engine
    # ==================================================================

    def add_policy_rule(self, rule: dict[str, Any]) -> None:
        """Add a declarative policy rule at runtime.

        Policy rules are dicts with at minimum a 'type' key specifying
        the rule kind (e.g. 'tool_deny', 'tool_allow', 'rate_limit').
        Additional keys depend on the rule type.

        Example:
            sdk.add_policy_rule({
                "type": "tool_deny",
                "tool_name": "shell_exec",
                "reason": "Shell disabled by policy",
            })
        """
        if "type" not in rule:
            raise ValueError("Policy rule must have a 'type' key")

        self._policy_rules.append(rule)

        # Apply to framework config if running
        if self._setup_done and self._framework is not None:
            fw_config = getattr(self._framework, "_config", None)
            if fw_config is not None:
                policy_config = getattr(fw_config, "policy", None)
                if policy_config is not None and hasattr(policy_config, "rules"):
                    current_rules = list(policy_config.rules)
                    current_rules.append(rule)
                    object.__setattr__(policy_config, "rules", current_rules)

    def list_policy_rules(self) -> list[dict[str, Any]]:
        """List active policy rules.

        Returns a combined list of rules from SDK-level additions and
        framework-level policy configuration.
        """
        result: list[dict[str, Any]] = list(self._policy_rules)

        if self._setup_done and self._framework is not None:
            fw_config = getattr(self._framework, "_config", None)
            if fw_config is not None:
                policy_config = getattr(fw_config, "policy", None)
                if policy_config is not None:
                    fw_rules = getattr(policy_config, "rules", [])
                    # Merge framework rules not already in SDK-level list
                    for fw_rule in fw_rules:
                        if fw_rule not in result:
                            result.append(fw_rule)

        return result

    def clear_policy_rules(self) -> None:
        """Clear all policy rules added via the SDK.

        Also clears rules in the framework's policy config if running.
        """
        self._policy_rules.clear()

        if self._setup_done and self._framework is not None:
            fw_config = getattr(self._framework, "_config", None)
            if fw_config is not None:
                policy_config = getattr(fw_config, "policy", None)
                if policy_config is not None and hasattr(policy_config, "rules"):
                    object.__setattr__(policy_config, "rules", [])

    # ==================================================================
    # Fork (Sub-SDK)
    # ==================================================================

    def fork(self, config_overrides: dict[str, Any] | None = None) -> AgentSDK:
        """Create an independent child SDK instance with optional config overrides.

        The child starts with a copy of this SDK's configuration, with
        any provided overrides applied. The child has its own framework
        instance, tool registrations, and event subscriptions.

        The child is NOT automatically set up — call setup() or let it
        auto-initialize on first use.

        Usage:
            child = sdk.fork({"model_name": "gpt-4", "max_iterations": 10})
            result = await child.run("Solve this complex problem")
        """
        # Build child config from current config with overrides
        current_config_dict = self._config.model_dump()
        if config_overrides:
            current_config_dict.update(config_overrides)

        child_config = SDKConfig(**current_config_dict)
        child = AgentSDK(config=child_config)

        # Copy custom tool registrations so they are available in the child
        for callable_ref, tool_def in self._custom_tools:
            child._custom_tools.append((callable_ref, tool_def))

        # Copy policy rules
        child._policy_rules = list(self._policy_rules)

        return child

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

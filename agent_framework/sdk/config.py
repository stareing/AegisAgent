"""SDK configuration — single entry point for all framework settings.

SDKConfig provides a clean, flat configuration surface. It maps to
internal FrameworkConfig but hides implementation details.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SDKConfig(BaseModel):
    """Configuration for AgentSDK.

    All settings have sensible defaults. Only model_adapter_type and
    api_key are typically required for production use.
    """

    # ── Model ─────────────────────────────────────────────────────
    model_adapter_type: str = "litellm"
    model_name: str = "gpt-3.5-turbo"
    api_key: str | None = None
    api_base: str | None = None
    temperature: float = 1.0
    max_output_tokens: int = 4096
    session_mode: str = "stateless"  # "stateless" | "stateful"
    # Fallback models (tried in order when primary fails)
    fallback_models: list[dict[str, Any]] = Field(default_factory=list)
    circuit_breaker_enabled: bool = True

    # ── Agent behavior ────────────────────────────────────────────
    system_prompt: str = "You are a helpful assistant."
    max_iterations: int = 20
    approval_mode: str = "DEFAULT"  # "DEFAULT" | "AUTO_EDIT" | "PLAN"
    auto_approve_tools: bool = True

    # ── Context ───────────────────────────────────────────────────
    max_context_tokens: int = 8192
    compression_strategy: str = "SUMMARIZATION"  # "SUMMARIZATION" | "TRIMMING" | "HYBRID"
    reserve_for_output: int = 1024

    # ── Memory ────────────────────────────────────────────────────
    memory_enabled: bool = True
    memory_db_path: str = "data/memories.db"
    memory_store_type: str = "sqlite"  # "sqlite" | "postgresql" | "mongodb" | "neo4j"
    memory_connection_url: str | None = None
    auto_extract_memory: bool = True
    max_memories_in_context: int = 10

    # ── Tools ─────────────────────────────────────────────────────
    shell_enabled: bool = False
    sandbox_enabled: bool = False
    sandbox_auto_select: bool = False
    max_concurrent_tools: int = 5

    # ── Sub-agents ────────────────────────────────────────────────
    allow_spawn: bool = False
    max_sub_agents: int = 5
    max_concurrent_sub_agents: int = 3
    collection_strategy: str = "HYBRID"  # "SEQUENTIAL" | "BATCH_ALL" | "HYBRID"
    execution_mode: str = "progressive"  # "parallel" | "progressive"

    # ── MCP ───────────────────────────────────────────────────────
    mcp_config_file: str | None = None
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)

    # ── A2A ───────────────────────────────────────────────────────
    a2a_known_agents: list[dict[str, Any]] = Field(default_factory=list)

    # ── Policy ────────────────────────────────────────────────────
    policy_file: str | None = None
    policy_rules: list[dict[str, Any]] = Field(default_factory=list)

    # ── Skills ────────────────────────────────────────────────────
    skill_definitions: list[dict[str, Any]] = Field(default_factory=list)
    skill_directories: list[str] = Field(default_factory=list)

    # ── Hooks ─────────────────────────────────────────────────────
    # Hooks are registered programmatically via sdk.register_hook()

    # ── Plugins ───────────────────────────────────────────────────
    plugin_dirs: list[str] = Field(default_factory=list)
    enabled_plugins: list[str] = Field(default_factory=list)
    disabled_plugins: list[str] = Field(default_factory=list)

    # ── Long interaction ──────────────────────────────────────────
    enable_interactive_subagents: bool = True
    enable_suspend_resume: bool = True

    # ── Logging ───────────────────────────────────────────────────
    log_level: str = "INFO"
    log_dir: str = "logs"

    # ── Output ────────────────────────────────────────────────────
    output_format: str = "text"  # "text" | "json" | "stream_json"

    # ── Identity ──────────────────────────────────────────────────
    agent_name: str = ""
    agent_emoji: str = ""

    def to_framework_config(self) -> dict[str, Any]:
        """Convert to internal FrameworkConfig dict representation."""
        return {
            "model": {
                "adapter_type": self.model_adapter_type,
                "default_model_name": self.model_name,
                "api_key": self.api_key,
                "api_base": self.api_base,
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
                "session_mode": self.session_mode,
                "fallback_models": self.fallback_models,
                "circuit_breaker_enabled": self.circuit_breaker_enabled,
            },
            "context": {
                "max_context_tokens": self.max_context_tokens,
                "default_compression_strategy": self.compression_strategy,
                "reserve_for_output": self.reserve_for_output,
            },
            "memory": {
                "enable_saved_memory": self.memory_enabled,
                "db_path": self.memory_db_path,
                "store_type": self.memory_store_type,
                "connection_url": self.memory_connection_url,
                "auto_extract_memory": self.auto_extract_memory,
                "max_memories_in_context": self.max_memories_in_context,
            },
            "tools": {
                "shell_enabled": self.shell_enabled,
                "sandbox_enabled": self.sandbox_enabled,
                "sandbox_auto_select": self.sandbox_auto_select,
                "max_concurrent_tool_calls": self.max_concurrent_tools,
                "approval_mode": self.approval_mode,
            },
            "subagent": {
                "max_sub_agents_per_run": self.max_sub_agents,
                "max_concurrent_sub_agents": self.max_concurrent_sub_agents,
                "default_collection_strategy": self.collection_strategy,
                "execution_mode": self.execution_mode,
            },
            "mcp": {
                "config_file": self.mcp_config_file,
                "servers": self.mcp_servers,
            },
            "a2a": {
                "known_agents": self.a2a_known_agents,
            },
            "policy": {
                "enabled": bool(self.policy_file or self.policy_rules),
                "policy_file": self.policy_file or "",
                "rules": self.policy_rules,
            },
            "skills": {
                "definitions": self.skill_definitions,
                "directories": self.skill_directories,
            },
            "plugins": {
                "plugin_dirs": self.plugin_dirs,
                "enabled_plugins": self.enabled_plugins,
                "disabled_plugins": self.disabled_plugins,
            },
            "long_interaction": {
                "enable_interactive_subagents": self.enable_interactive_subagents,
                "enable_suspend_resume": self.enable_suspend_resume,
            },
            "output": {
                "format": self.output_format,
            },
            "logging": {
                "level": self.log_level,
                "log_dir": self.log_dir,
            },
            "identity": {
                "name": self.agent_name,
                "emoji": self.agent_emoji,
            },
        }

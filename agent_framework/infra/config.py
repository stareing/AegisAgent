from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class ModelConfig(BaseModel):
    adapter_type: str = "litellm"  # "litellm"|"openai"|"anthropic"|"google"|"deepseek"|"doubao"|"qwen"|"zhipu"|"minimax"|"custom"
    default_model_name: str = "gpt-3.5-turbo"
    temperature: float = 1.0
    max_output_tokens: int = 4096
    api_key: str | None = None
    api_base: str | None = None
    timeout_ms: int = 30000
    max_retries: int = 3
    session_mode: str = "stateless"  # "stateless" | "stateful"


class ContextConfig(BaseModel):
    """Context budget configuration.

    Quota semantics:
    - max_context_tokens: SOFT — exceeded triggers compression/trimming, not abort
    - reserve_for_output: SOFT — best-effort reservation, not enforced by LLM
    - spawn_seed_ratio: SOFT — child context seed ratio, trimmed if over budget
    """

    max_context_tokens: int = 8192
    reserve_for_output: int = 1024
    compress_threshold_ratio: float = 0.85
    default_compression_strategy: str = "LLM_SUMMARIZE"
    spawn_seed_ratio: float = 0.3


class MemoryConfig(BaseModel):
    """Memory configuration.

    Quota semantics:
    - max_memories_in_context: SOFT — excess memories are trimmed by relevance, not error
    - max_memory_items_per_user: SOFT — oldest/lowest-priority purged on overflow

    store_type: "sqlite" (default) | "postgresql" | "mongodb"
    - sqlite: uses db_path
    - postgresql: uses connection_url (e.g. "postgresql+asyncpg://user:pass@host/db")
    - mongodb: uses connection_url (e.g. "mongodb://host:27017/db")
    """

    store_type: str = "sqlite"
    db_path: str = "data/memories.db"
    connection_url: str | None = None
    database_name: str | None = None
    neo4j_auth: str | None = None  # "user:password" for neo4j
    enable_saved_memory: bool = True
    auto_extract_memory: bool = True
    max_memories_in_context: int = 10
    max_memory_items_per_user: int = 200
    allow_user_memory_namespace: bool = True
    allow_memory_management_api: bool = True


class ToolConfig(BaseModel):
    """Tool execution configuration.

    Quota semantics:
    - max_concurrent_tool_calls: SOFT — excess queued via semaphore, not rejected
    """

    confirmation_handler_type: str = "cli"
    max_concurrent_tool_calls: int = 5
    allow_parallel_tool_calls: bool = True


class SubAgentConfig(BaseModel):
    """Sub-agent configuration.

    Quota semantics — HARD vs SOFT:

    HARD (exceed → immediate reject/abort, no degraded mode):
    - max_sub_agents_per_run     — spawn denied with QUOTA_EXCEEDED
    - allow_recursive_spawn      — children cannot spawn (PERMISSION_DENIED)
    - default_deadline_ms        — sub-agent killed on timeout (TIMEOUT)
    - default_max_iterations     — sub-agent forcefully stopped

    SOFT (exceed → graceful degradation, trimming, or warning):
    - per_sub_agent_max_tokens   — context trimmed if over budget
    - max_concurrent_sub_agents  — excess queued, not rejected
    """

    max_sub_agents_per_run: int = 5
    max_concurrent_sub_agents: int = 3
    per_sub_agent_max_tokens: int = 4096
    default_deadline_ms: int = 60000
    default_max_iterations: int = 10
    allow_recursive_spawn: bool = False
    max_spawn_depth: int = 1
    execution_mode: str = "progressive"  # "parallel" | "progressive"


class SkillConfig(BaseModel):
    """Declarative skill definition loaded from config JSON."""
    skill_id: str
    name: str = ""
    description: str = ""
    trigger_keywords: list[str] = Field(default_factory=list)
    system_prompt_addon: str = ""
    model_override: str | None = None
    temperature_override: float | None = None


class SkillsConfig(BaseModel):
    """Container for skill definitions in config."""
    definitions: list[SkillConfig] = Field(default_factory=list)
    directories: list[str] = Field(default_factory=list)


class MCPConfig(BaseModel):
    config_file: str | None = None
    servers: list[dict] = Field(default_factory=list)


class A2AConfig(BaseModel):
    known_agents: list[dict] = Field(default_factory=list)


class LoggingConfig(BaseModel):
    log_dir: str = "logs"
    json_output: bool = True
    level: str = "INFO"


class FrameworkConfig(BaseSettings):
    """Root framework configuration.

    Quota ownership table (v2.5.2 §26):
    Each quota has exactly one OWNER module that enforces it.

    ┌──────────────────────────────┬──────────┬─────────────────────────┐
    │ Quota                        │ Severity │ Owner                   │
    ├──────────────────────────────┼──────────┼─────────────────────────┤
    │ AgentConfig.max_iterations   │ HARD     │ AgentLoop (stop check)  │
    │ AgentConfig.max_output_tokens│ SOFT     │ LLM adapter (truncation)│
    │ AgentConfig.allow_spawn      │ HARD     │ DelegationExecutor      │
    │ SubAgent.max_per_run         │ HARD     │ SubAgentScheduler       │
    │ SubAgent.max_concurrent      │ SOFT     │ SubAgentScheduler       │
    │ SubAgent.deadline_ms         │ HARD     │ SubAgentScheduler       │
    │ SubAgent.max_iterations      │ HARD     │ AgentLoop (sub run)     │
    │ SubAgent.token_budget        │ SOFT     │ ContextBuilder (trim)   │
    │ Context.max_context_tokens   │ SOFT     │ ContextBuilder (trim)   │
    │ Context.reserve_for_output   │ SOFT     │ ContextBuilder          │
    │ Memory.max_in_context        │ SOFT     │ MemoryManager (trim)    │
    │ Memory.max_per_user          │ SOFT     │ MemoryManager (purge)   │
    │ Tool.max_concurrent          │ SOFT     │ ToolExecutor (semaphore)│
    └──────────────────────────────┴──────────┴─────────────────────────┘

    Rule: If you need to check a quota, you MUST go through its owner.
    No module may read another module's quota and enforce it independently.
    """

    model: ModelConfig = Field(default_factory=ModelConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    subagent: SubAgentConfig = Field(default_factory=SubAgentConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    mcp: MCPConfig = Field(default_factory=MCPConfig)
    a2a: A2AConfig = Field(default_factory=A2AConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    model_config = {"env_prefix": "AGENT_", "env_nested_delimiter": "__"}


_current_config: FrameworkConfig | None = None
_current_config_path: str | Path | None = None


def load_config(config_path: str | Path | None = None) -> FrameworkConfig:
    """Load config from JSON file, falling back to defaults."""
    global _current_config, _current_config_path
    _current_config_path = config_path
    if config_path and Path(config_path).exists():
        with open(config_path) as f:
            data = json.load(f)
        _current_config = FrameworkConfig(**data)
    else:
        _current_config = FrameworkConfig()
    return _current_config


def reload_config() -> None:
    """Reload config from the last used config path."""
    global _current_config
    _current_config = load_config(_current_config_path)

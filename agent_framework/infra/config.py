from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class ModelConfig(BaseModel):
    default_model_name: str = "gpt-3.5-turbo"
    temperature: float = 0.7
    max_output_tokens: int = 4096
    api_base: str | None = None
    timeout_ms: int = 30000
    max_retries: int = 3


class ContextConfig(BaseModel):
    max_context_tokens: int = 8192
    reserve_for_output: int = 1024
    compress_threshold_ratio: float = 0.85
    default_compression_strategy: str = "SLIDING_WINDOW"
    spawn_seed_ratio: float = 0.3


class MemoryConfig(BaseModel):
    db_path: str = "data/memories.db"
    enable_saved_memory: bool = True
    auto_extract_memory: bool = True
    max_memories_in_context: int = 10
    max_memory_items_per_user: int = 200
    allow_user_memory_namespace: bool = True
    allow_memory_management_api: bool = True


class ToolConfig(BaseModel):
    confirmation_handler_type: str = "cli"
    max_concurrent_tool_calls: int = 5
    allow_parallel_tool_calls: bool = True


class SubAgentConfig(BaseModel):
    max_sub_agents_per_run: int = 5
    max_concurrent_sub_agents: int = 3
    per_sub_agent_max_tokens: int = 4096
    default_deadline_ms: int = 60000
    default_max_iterations: int = 10
    allow_recursive_spawn: bool = False


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
    model: ModelConfig = Field(default_factory=ModelConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    subagent: SubAgentConfig = Field(default_factory=SubAgentConfig)
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

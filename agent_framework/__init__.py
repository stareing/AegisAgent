# Agent Framework — Extensible AI Agent Framework
#
# Usage:
#   from agent_framework import AgentFramework, FrameworkConfig, tool
#   from agent_framework import StateGraph, START, END          # graph
#   from agent_framework import Message, AgentRunResult         # models
from agent_framework.agent.base_agent import BaseAgent
from agent_framework.agent.default_agent import DefaultAgent
from agent_framework.agent.orchestrator_agent import OrchestratorAgent
from agent_framework.entry import AgentFramework
from agent_framework.graph import (END, START, CheckpointerProtocol,
                                   CompiledGraph, GraphStreamEvent,
                                   InMemorySaver, StateGraph, StreamMode,
                                   agent_node, branch_node, passthrough_node,
                                   tool_node)
from agent_framework.infra.config import (A2AConfig, ContextConfig,
                                          FrameworkConfig, LoggingConfig,
                                          MCPConfig, MemoryConfig, ModelConfig,
                                          SkillsConfig, SubAgentConfig,
                                          ToolConfig, TracingConfig,
                                          load_config, reload_config)
from agent_framework.models.agent import (AgentConfig, AgentRunResult,
                                          AgentState, AgentStatus,
                                          CapabilityPolicy, ContextPolicy,
                                          EffectiveRunConfig, ErrorStrategy,
                                          IterationResult, MemoryPolicy,
                                          MemoryQuota, Skill, StopDecision,
                                          StopReason, StopSignal,
                                          TerminationKind)
from agent_framework.models.context import ContextStats, LLMRequest
from agent_framework.models.memory import (MemoryCandidate, MemoryKind,
                                           MemoryRecord, MemoryUpdateAction)
from agent_framework.models.message import (ContentPart, Message,
                                            ModelResponse, TokenUsage,
                                            ToolCallRequest)
from agent_framework.models.session import SessionState
from agent_framework.models.stream import StreamEvent, StreamEventType
from agent_framework.models.subagent import (Artifact, DelegationSummary,
                                             MemoryScope, SpawnMode,
                                             SubAgentResult, SubAgentSpec)
from agent_framework.models.tool import (ToolEntry, ToolExecutionMeta,
                                         ToolMeta, ToolResult)
from agent_framework.protocols.core import (ContextEngineerProtocol,
                                            DelegationExecutorProtocol,
                                            MemoryManagerProtocol,
                                            MemoryStoreProtocol,
                                            ModelAdapterProtocol,
                                            ToolExecutorProtocol,
                                            ToolRegistryProtocol)
from agent_framework.tools.confirmation import (AutoApproveConfirmationHandler,
                                                CLIConfirmationHandler)
from agent_framework.tools.decorator import tool

__version__ = "0.1.0"

# ── Core facade ────────────────────────────────────────────────────


# ── Configuration ──────────────────────────────────────────────────


# ── Agent base classes ─────────────────────────────────────────────


# ── Tool decorator ─────────────────────────────────────────────────


# ── Confirmation handlers ──────────────────────────────────────────


# ── Streaming ──────────────────────────────────────────────────────


# ── Graph (LangGraph-compatible) ──────────────────────────────────


# ── Models — message & response ───────────────────────────────────


# ── Models — agent & run ──────────────────────────────────────────


# ── Models — session ──────────────────────────────────────────────


# ── Models — memory ───────────────────────────────────────────────


# ── Models — sub-agent ────────────────────────────────────────────


# ── Models — tool ─────────────────────────────────────────────────


# ── Models — context ──────────────────────────────────────────────


# ── Protocols ─────────────────────────────────────────────────────


# ── __all__ ────────────────────────────────────────────────────────

__all__ = [
    "__version__",
    # Core
    "AgentFramework",
    # Config
    "FrameworkConfig",
    "ModelConfig",
    "ContextConfig",
    "MemoryConfig",
    "ToolConfig",
    "SubAgentConfig",
    "SkillsConfig",
    "MCPConfig",
    "A2AConfig",
    "LoggingConfig",
    "TracingConfig",
    "load_config",
    "reload_config",
    # Agents
    "BaseAgent",
    "DefaultAgent",
    "OrchestratorAgent",
    # Tool decorator
    "tool",
    # Confirmation
    "CLIConfirmationHandler",
    "AutoApproveConfirmationHandler",
    # Streaming
    "StreamEvent",
    "StreamEventType",
    # Graph
    "StateGraph",
    "CompiledGraph",
    "GraphStreamEvent",
    "InMemorySaver",
    "CheckpointerProtocol",
    "START",
    "END",
    "StreamMode",
    "agent_node",
    "tool_node",
    "passthrough_node",
    "branch_node",
    # Message models
    "Message",
    "ContentPart",
    "ModelResponse",
    "TokenUsage",
    "ToolCallRequest",
    # Agent models
    "AgentConfig",
    "AgentRunResult",
    "AgentState",
    "AgentStatus",
    "CapabilityPolicy",
    "ContextPolicy",
    "EffectiveRunConfig",
    "ErrorStrategy",
    "IterationResult",
    "MemoryPolicy",
    "MemoryQuota",
    "Skill",
    "StopDecision",
    "StopReason",
    "StopSignal",
    "TerminationKind",
    # Session
    "SessionState",
    # Memory models
    "MemoryCandidate",
    "MemoryKind",
    "MemoryRecord",
    "MemoryUpdateAction",
    # SubAgent models
    "Artifact",
    "DelegationSummary",
    "MemoryScope",
    "SpawnMode",
    "SubAgentResult",
    "SubAgentSpec",
    # Tool models
    "ToolEntry",
    "ToolExecutionMeta",
    "ToolMeta",
    "ToolResult",
    # Context
    "ContextStats",
    "LLMRequest",
    # Protocols
    "ModelAdapterProtocol",
    "ToolRegistryProtocol",
    "ToolExecutorProtocol",
    "DelegationExecutorProtocol",
    "MemoryStoreProtocol",
    "MemoryManagerProtocol",
    "ContextEngineerProtocol",
]

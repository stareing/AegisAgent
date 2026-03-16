"""Hooks subsystem — governed extension points for the agent framework.

Hooks are NOT arbitrary callbacks. They execute at framework-predefined
points, receive stable DTO snapshots, and can only perform whitelisted
actions. Hook results never bypass the main state machine.

Three hook categories:
- Command Hook: deterministic gate (pre/post checks, audit)
- Prompt Hook: advisory (LLM-assisted suggestions)
- Agent Hook: complex logic via controlled sub-agent

Design invariants:
- Hooks consume DTO snapshots only — never mutable state objects
- Hook side effects go through the unified commit chain
- Disabled plugins' hooks are never executed
- Execution order is stable: priority → plugin_id → hook_id
"""

from agent_framework.hooks.models import (
    HookCategory,
    HookContext,
    HookExecutionMode,
    HookFailurePolicy,
    HookMeta,
    HookPoint,
    HookResult,
    HookResultAction,
)
from agent_framework.hooks.protocol import AsyncHookProtocol, HookProtocol
from agent_framework.hooks.registry import HookRegistry
from agent_framework.hooks.executor import HookExecutor
from agent_framework.hooks.interpreter import HookChainOutcome, interpret_hook_results
from agent_framework.hooks.singleton import HookSubsystem

__all__ = [
    "HookCategory",
    "HookContext",
    "HookExecutionMode",
    "HookFailurePolicy",
    "HookMeta",
    "HookPoint",
    "HookResult",
    "HookResultAction",
    "AsyncHookProtocol",
    "HookProtocol",
    "HookRegistry",
    "HookExecutor",
    "HookChainOutcome",
    "interpret_hook_results",
    "HookSubsystem",
]

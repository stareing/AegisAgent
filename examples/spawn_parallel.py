"""Example: Spawning parallel sub-agents.

Demonstrates how to use the sub-agent runtime to spawn multiple
child agents for parallel task execution.
"""

import asyncio

from agent_framework.agent.default_agent import DefaultAgent
from agent_framework.agent.coordinator import RunCoordinator
from agent_framework.agent.runtime_deps import AgentRuntimeDeps
from agent_framework.agent.skill_router import SkillRouter
from agent_framework.adapters.model.litellm_adapter import LiteLLMAdapter
from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.engineer import ContextEngineer
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.memory.default_manager import DefaultMemoryManager
from agent_framework.memory.sqlite_store import SQLiteMemoryStore
from agent_framework.models.subagent import MemoryScope, SpawnMode, SubAgentSpec
from agent_framework.subagent.runtime import SubAgentRuntime
from agent_framework.tools.confirmation import AutoApproveConfirmationHandler
from agent_framework.tools.executor import ToolExecutor
from agent_framework.tools.registry import ToolRegistry


def _build_deps() -> AgentRuntimeDeps:
    """Build a minimal set of runtime dependencies."""
    store = SQLiteMemoryStore(db_path=":memory:")
    memory = DefaultMemoryManager(store=store)
    model = LiteLLMAdapter(model_name="gpt-3.5-turbo")
    registry = ToolRegistry()
    executor = ToolExecutor(registry=registry)
    source = ContextSourceProvider()
    builder = ContextBuilder()
    compressor = ContextCompressor()
    engineer = ContextEngineer(source_provider=source, builder=builder, compressor=compressor)

    return AgentRuntimeDeps(
        tool_registry=registry,
        tool_executor=executor,
        memory_manager=memory,
        context_engineer=engineer,
        model_adapter=model,
        skill_router=SkillRouter(),
        confirmation_handler=AutoApproveConfirmationHandler(),
    )


async def main():
    deps = _build_deps()
    coordinator = RunCoordinator()

    # Create sub-agent runtime
    runtime = SubAgentRuntime(
        parent_deps=deps,
        coordinator=coordinator,
        max_concurrent=3,
        max_per_run=5,
    )

    # Define tasks for parallel execution
    tasks = [
        SubAgentSpec(
            parent_run_id="main_run",
            spawn_id=f"task_{i}",
            mode=SpawnMode.EPHEMERAL,
            task_input=task_text,
            memory_scope=MemoryScope.ISOLATED,
            max_iterations=5,
            deadline_ms=30000,
        )
        for i, task_text in enumerate([
            "What is 2 + 2?",
            "What is the capital of France?",
            "List 3 primary colors.",
        ])
    ]

    # Spawn all in parallel
    results = await asyncio.gather(*[
        runtime.spawn(spec, None) for spec in tasks
    ])

    for spec, result in zip(tasks, results):
        print(f"[{spec.spawn_id}] Success={result.success}, Answer={result.final_answer}")


if __name__ == "__main__":
    asyncio.run(main())

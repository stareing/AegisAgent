from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import SubAgentHandle, SubAgentResult, SubAgentSpec
from agent_framework.subagent.factory import SubAgentFactory
from agent_framework.subagent.scheduler import SubAgentScheduler

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.agent.coordinator import RunCoordinator
    from agent_framework.agent.runtime_deps import AgentRuntimeDeps

logger = get_logger(__name__)


class SubAgentRuntime:
    """Facade composing SubAgentFactory + SubAgentScheduler.

    Implements SubAgentRuntimeProtocol:
    - spawn(spec, parent_agent) -> SubAgentResult
    - get_active_children(parent_run_id) -> list[SubAgentHandle]
    - cancel_all(parent_run_id) -> int
    """

    def __init__(
        self,
        parent_deps: AgentRuntimeDeps,
        coordinator: RunCoordinator | None = None,
        max_concurrent: int = 3,
        max_per_run: int = 5,
    ) -> None:
        self._factory = SubAgentFactory(parent_deps)
        self._scheduler = SubAgentScheduler(
            max_concurrent=max_concurrent,
            max_per_run=max_per_run,
        )
        self._coordinator = coordinator
        self._parent_deps = parent_deps

    async def spawn(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult:
        """Spawn a sub-agent and wait for its result."""
        # Assign spawn_id if not set
        if not spec.spawn_id:
            spec.spawn_id = uuid.uuid4().hex[:12]

        parent_id = getattr(parent_agent, "agent_id", "unknown") if parent_agent else "none"

        logger.info(
            "subagent.spawning",
            spawn_id=spec.spawn_id,
            parent_agent_id=parent_id,
            task_input=spec.task_input[:150],
            mode=spec.mode.value if hasattr(spec.mode, "value") else str(spec.mode),
            memory_scope=spec.memory_scope.value if hasattr(spec.memory_scope, "value") else str(spec.memory_scope),
            deadline_ms=spec.deadline_ms,
        )

        # Create handle
        handle = SubAgentHandle(
            sub_agent_id=f"sub_{spec.spawn_id}",
            spawn_id=spec.spawn_id,
            parent_run_id=spec.parent_run_id,
            status="PENDING",
            created_at=datetime.now(timezone.utc),
        )

        # Create sub-agent and deps
        logger.info(
            "subagent.creating",
            spawn_id=spec.spawn_id,
            step="factory.create_agent_and_deps",
        )
        sub_agent, sub_deps = self._factory.create_agent_and_deps(
            spec, parent_agent
        )
        logger.info(
            "subagent.created",
            spawn_id=spec.spawn_id,
            sub_agent_id=sub_agent.agent_id,
            tools_count=len(sub_deps.tool_registry.list_tools()),
        )

        # Doc 16.2 step 3: ensure spawn seed is built for child run context.
        if spec.context_seed is None:
            spec.context_seed = self._parent_deps.context_engineer.build_spawn_seed(
                session_messages=[],
                query=spec.task_input,
                token_budget=spec.token_budget,
            )
            logger.info(
                "subagent.context_seed_built",
                spawn_id=spec.spawn_id,
                seed_messages=len(spec.context_seed) if spec.context_seed else 0,
            )

        # Avoid duplicating the current child query: it is provided as `task_input`.
        initial_session_messages = list(spec.context_seed or [])
        if (
            initial_session_messages
            and initial_session_messages[-1].role == "user"
            and (initial_session_messages[-1].content or "") == spec.task_input
        ):
            initial_session_messages = initial_session_messages[:-1]

        # Get or create coordinator
        coordinator = self._coordinator
        if coordinator is None:
            from agent_framework.agent.coordinator import RunCoordinator
            coordinator = RunCoordinator()

        # Check quota before scheduling
        quota = self._scheduler.get_quota_status(spec.parent_run_id)
        logger.info(
            "subagent.quota_check",
            spawn_id=spec.spawn_id,
            parent_run_id=spec.parent_run_id,
            total_spawned=quota["total_spawned"],
            quota_remaining=quota["quota_remaining"],
            active_count=quota["active_count"],
            max_concurrent=quota["max_concurrent"],
        )

        # Schedule execution
        async def _run() -> SubAgentResult:
            logger.info("subagent.run_started", spawn_id=spec.spawn_id)
            run_result = await coordinator.run(
                sub_agent,
                sub_deps,
                spec.task_input,
                initial_session_messages=initial_session_messages,
            )
            logger.info(
                "subagent.run_finished",
                spawn_id=spec.spawn_id,
                success=run_result.success,
                iterations_used=run_result.iterations_used,
                total_tokens=run_result.usage.total_tokens,
            )
            return SubAgentResult(
                spawn_id=spec.spawn_id,
                success=run_result.success,
                final_answer=run_result.final_answer,
                error=run_result.error,
                usage=run_result.usage,
                iterations_used=run_result.iterations_used,
            )

        result = await self._scheduler.schedule(
            handle, _run(), deadline_ms=spec.deadline_ms
        )

        logger.info(
            "subagent.spawn_completed",
            spawn_id=spec.spawn_id,
            success=result.success,
            duration_ms=result.duration_ms,
            iterations_used=result.iterations_used,
            answer_preview=(result.final_answer or result.error or "")[:120],
        )
        return result

    def get_active_children(self, parent_run_id: str) -> list[SubAgentHandle]:
        return self._scheduler.get_active_children(parent_run_id)

    async def cancel_all(self, parent_run_id: str) -> int:
        return await self._scheduler.cancel_all(parent_run_id)

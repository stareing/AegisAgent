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

        # Create handle
        handle = SubAgentHandle(
            sub_agent_id=f"sub_{spec.spawn_id}",
            spawn_id=spec.spawn_id,
            parent_run_id=spec.parent_run_id,
            status="PENDING",
            created_at=datetime.now(timezone.utc),
        )

        # Create sub-agent and deps
        sub_agent, sub_deps = self._factory.create_agent_and_deps(
            spec, parent_agent
        )

        # Get or create coordinator
        coordinator = self._coordinator
        if coordinator is None:
            from agent_framework.agent.coordinator import RunCoordinator
            coordinator = RunCoordinator()

        # Schedule execution
        async def _run() -> SubAgentResult:
            run_result = await coordinator.run(
                sub_agent, sub_deps, spec.task_input
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
        )
        return result

    def get_active_children(self, parent_run_id: str) -> list[SubAgentHandle]:
        return self._scheduler.get_active_children(parent_run_id)

    async def cancel_all(self, parent_run_id: str) -> int:
        return await self._scheduler.cancel_all(parent_run_id)

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import (SubAgentHandle, SubAgentResult,
                                             SubAgentSpec, SubAgentStatus,
                                             SubAgentSuspendInfo,
                                             SubAgentSuspendReason,
                                             SubAgentTaskStatus)
from agent_framework.subagent.factory import SubAgentFactory
from agent_framework.subagent.scheduler import SubAgentScheduler

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.agent.coordinator import RunCoordinator
    from agent_framework.agent.runtime_deps import AgentRuntimeDeps

logger = get_logger(__name__)


class SubAgentRuntime:
    """Executes sub-agent runs and manages their lifecycle.

    Ownership boundary (v2.6.3 §39):
    - Responsible for: starting sub-agent runs, maintaining active_children
      truth source, executing cancel propagation, resource cleanup,
      producing SubAgentResult, assigning child_run_id
    - NOT responsible for: quota decisions, queuing, generating subagent_task_id

    SubAgentScheduler handles queuing/quota/concurrency.
    SubAgentRuntime handles execution/lifecycle/cancellation.

    active_children truth source: this class only (not duplicated in scheduler).

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
        max_spawn_depth: int = 1,
    ) -> None:
        self._factory = SubAgentFactory(parent_deps)
        self._scheduler = SubAgentScheduler(
            max_concurrent=max_concurrent,
            max_per_run=max_per_run,
        )
        self._coordinator = coordinator
        self._parent_deps = parent_deps
        self._max_spawn_depth = max_spawn_depth
        # active_children truth source — only SubAgentRuntime maintains this
        self._active: dict[str, SubAgentHandle] = {}  # spawn_id -> handle

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

        # Allocate task record via scheduler (task_id from scheduler only)
        task_record = self._scheduler.allocate_task_id(
            spec.parent_run_id, spec.spawn_id
        )

        # Create handle
        handle = SubAgentHandle(
            sub_agent_id=f"sub_{spec.spawn_id}",
            spawn_id=spec.spawn_id,
            parent_run_id=spec.parent_run_id,
            status="PENDING",
            created_at=datetime.now(timezone.utc),
        )

        # Register in active_children (runtime is the truth source)
        self._active[spec.spawn_id] = handle

        # Create sub-agent and deps
        logger.info(
            "subagent.creating",
            spawn_id=spec.spawn_id,
            step="factory.create_agent_and_deps",
            task_id=task_record.subagent_task_id,
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
            max_concurrent=quota["max_concurrent"],
        )

        # Schedule execution — runtime wraps the actual run
        async def _run() -> SubAgentResult:
            child_run_id = str(uuid.uuid4())
            task_record.child_run_id = child_run_id
            task_record.status = SubAgentTaskStatus.RUNNING

            logger.info(
                "subagent.run_started",
                spawn_id=spec.spawn_id,
                child_run_id=child_run_id,
                task_id=task_record.subagent_task_id,
            )
            run_result = await coordinator.run(
                sub_agent,
                sub_deps,
                spec.task_input,
                initial_session_messages=initial_session_messages,
            )

            task_record.status = (
                SubAgentTaskStatus.COMPLETED if run_result.success
                else SubAgentTaskStatus.FAILED
            )

            logger.info(
                "subagent.run_finished",
                spawn_id=spec.spawn_id,
                child_run_id=child_run_id,
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
        try:
            result = await self._scheduler.schedule(
                handle, _run(), deadline_ms=spec.deadline_ms,
                task_record=task_record,
            )
        finally:
            # Remove from active_children after completion (runtime cleanup)
            self._active.pop(spec.spawn_id, None)

        # Handle cancellation status from scheduler
        if handle.status == "CANCELLED":
            task_record.status = SubAgentTaskStatus.CANCELLED

        logger.info(
            "subagent.spawn_completed",
            spawn_id=spec.spawn_id,
            success=result.success,
            duration_ms=result.duration_ms,
            iterations_used=result.iterations_used,
            task_status=task_record.status.value,
            answer_preview=(result.final_answer or result.error or "")[:120],
        )
        return result

    async def spawn_async(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> str:
        """Spawn a sub-agent without waiting. Returns spawn_id immediately.

        Uses scheduler.submit() (non-blocking) instead of schedule() (blocking).
        Call collect_result(spawn_id) later to get the result.
        """
        if not spec.spawn_id:
            spec.spawn_id = uuid.uuid4().hex[:12]

        parent_id = getattr(parent_agent, "agent_id", "unknown") if parent_agent else "none"

        logger.info(
            "subagent.spawning_async",
            spawn_id=spec.spawn_id,
            parent_agent_id=parent_id,
            task_input=spec.task_input[:150],
        )

        task_record = self._scheduler.allocate_task_id(
            spec.parent_run_id, spec.spawn_id
        )

        handle = SubAgentHandle(
            sub_agent_id=f"sub_{spec.spawn_id}",
            spawn_id=spec.spawn_id,
            parent_run_id=spec.parent_run_id,
            status="PENDING",
            created_at=datetime.now(timezone.utc),
        )
        self._active[spec.spawn_id] = handle

        try:
            # create_agent_and_deps hits SQLite synchronously for snapshot/store creation
            import asyncio
            sub_agent, sub_deps = await asyncio.to_thread(
                self._factory.create_agent_and_deps, spec, parent_agent
            )
        except Exception:
            self._active.pop(spec.spawn_id, None)
            raise

        if spec.context_seed is None:
            spec.context_seed = self._parent_deps.context_engineer.build_spawn_seed(
                session_messages=[], query=spec.task_input,
                token_budget=spec.token_budget,
            )

        initial_session_messages = list(spec.context_seed or [])
        if (
            initial_session_messages
            and initial_session_messages[-1].role == "user"
            and (initial_session_messages[-1].content or "") == spec.task_input
        ):
            initial_session_messages = initial_session_messages[:-1]

        coordinator = self._coordinator
        if coordinator is None:
            from agent_framework.agent.coordinator import RunCoordinator
            coordinator = RunCoordinator()

        async def _run() -> SubAgentResult:
            child_run_id = str(uuid.uuid4())
            task_record.child_run_id = child_run_id
            task_record.status = SubAgentTaskStatus.RUNNING
            try:
                run_result = await coordinator.run(
                    sub_agent, sub_deps, spec.task_input,
                    initial_session_messages=initial_session_messages,
                )
                task_record.status = (
                    SubAgentTaskStatus.COMPLETED if run_result.success
                    else SubAgentTaskStatus.FAILED
                )
                return SubAgentResult(
                    spawn_id=spec.spawn_id, success=run_result.success,
                    final_answer=run_result.final_answer, error=run_result.error,
                    usage=run_result.usage, iterations_used=run_result.iterations_used,
                )
            except Exception as e:
                task_record.status = SubAgentTaskStatus.FAILED
                return SubAgentResult(
                    spawn_id=spec.spawn_id, success=False, error=str(e),
                )
            finally:
                self._active.pop(spec.spawn_id, None)

        # submit() returns immediately — task runs in background
        coro = _run()
        try:
            self._scheduler.submit(
                handle, coro, deadline_ms=spec.deadline_ms,
                task_record=task_record,
            )
        except Exception:
            self._active.pop(spec.spawn_id, None)
            coro.close()
            raise

        logger.info(
            "subagent.async_submitted",
            spawn_id=spec.spawn_id,
            task_id=task_record.subagent_task_id,
        )
        return spec.spawn_id

    async def collect_result(
        self, spawn_id: str, wait: bool = True
    ) -> SubAgentResult | None:
        """Collect result of an async sub-agent.

        Args:
            spawn_id: The spawn_id returned by spawn_async.
            wait: If True, block until complete. If False, return None if still running.

        Returns:
            SubAgentResult if complete, None if still running (wait=False only).
        """
        handle = self._active.get(spawn_id)

        # Already completed and cleaned up — check scheduler results
        if handle is None:
            result = self._scheduler.get_result_if_ready(spawn_id)
            if result is not None:
                return result
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error=f"No active sub-agent with spawn_id={spawn_id}",
            )

        if not wait:
            if self._scheduler.is_running(spawn_id):
                return None  # Still running
            result = self._scheduler.get_result_if_ready(spawn_id)
            if result is not None:
                self._active.pop(spawn_id, None)
                return result
            return None

        # Blocking: await the scheduler result
        result = await self._scheduler.await_result(handle)
        self._active.pop(spawn_id, None)
        return result

    def get_active_children(self, parent_run_id: str) -> list[SubAgentHandle]:
        """Return active children. This is the SOLE truth source (v2.6.3 §39)."""
        return [
            h for h in self._active.values()
            if h.parent_run_id == parent_run_id
        ]

    async def resume(
        self,
        spawn_id: str,
        resume_payload: dict,
        parent_agent: Any,
    ) -> SubAgentResult:
        """Resume a suspended/waiting sub-agent with additional input.

        The resume_payload is injected as context for the next execution phase.
        If the sub-agent is not in a resumable state, returns an error result.

        Boundary §7: This is true resume only if the runtime has preserved
        the execution context. If the original run completed/failed, this
        creates a follow-up run with resume_payload as task context.
        """
        handle = self._active.get(spawn_id)

        # If there's no active handle, the agent already completed or was never spawned
        if handle is None:
            logger.warning(
                "subagent.resume.not_found",
                spawn_id=spawn_id,
            )
            return SubAgentResult(
                spawn_id=spawn_id,
                success=False,
                error=f"No active sub-agent with spawn_id={spawn_id} to resume",
            )

        # Check if the sub-agent is in a resumable state
        resumable_statuses = {
            SubAgentStatus.WAITING_PARENT,
            SubAgentStatus.WAITING_USER,
            SubAgentStatus.SUSPENDED,
        }
        if handle.status not in resumable_statuses:
            logger.warning(
                "subagent.resume.not_resumable",
                spawn_id=spawn_id,
                current_status=handle.status.value if hasattr(handle.status, "value") else str(handle.status),
            )
            return SubAgentResult(
                spawn_id=spawn_id,
                success=False,
                error=f"Cannot resume sub-agent in status {handle.status}",
            )

        handle.status = SubAgentStatus.RESUMING
        logger.info(
            "subagent.resuming",
            spawn_id=spawn_id,
            resume_keys=list(resume_payload.keys()),
        )

        # Build a follow-up task with resume context
        resume_task = resume_payload.get("answer", resume_payload.get("input", str(resume_payload)))

        coordinator = self._coordinator
        if coordinator is None:
            from agent_framework.agent.coordinator import RunCoordinator
            coordinator = RunCoordinator()

        # Re-create sub-agent for the resume phase
        original_spec = SubAgentSpec(
            parent_run_id=handle.parent_run_id,
            spawn_id=spawn_id,
            task_input=f"[Resume from previous phase] {resume_task}",
        )

        try:
            sub_agent, sub_deps = self._factory.create_agent_and_deps(
                original_spec,
                parent_agent,
            )
        except Exception as e:
            handle.status = SubAgentStatus.FAILED
            return SubAgentResult(
                spawn_id=spawn_id,
                success=False,
                error=f"Failed to create sub-agent for resume: {e}",
            )

        handle.status = SubAgentStatus.RUNNING

        try:
            run_result = await coordinator.run(
                sub_agent,
                sub_deps,
                original_spec.task_input,
            )
            final_status = SubAgentStatus.COMPLETED if run_result.success else SubAgentStatus.FAILED
            handle.status = final_status

            result = SubAgentResult(
                spawn_id=spawn_id,
                success=run_result.success,
                final_status=final_status,
                final_answer=run_result.final_answer,
                error=run_result.error,
                usage=run_result.usage,
                iterations_used=run_result.iterations_used,
            )
        except Exception as e:
            handle.status = SubAgentStatus.FAILED
            result = SubAgentResult(
                spawn_id=spawn_id,
                success=False,
                final_status=SubAgentStatus.FAILED,
                error=f"Resume execution failed: {e}",
            )
        finally:
            self._active.pop(spawn_id, None)

        logger.info(
            "subagent.resume_completed",
            spawn_id=spawn_id,
            success=result.success,
        )
        return result

    async def cancel(self, spawn_id: str) -> None:
        """Cancel a single sub-agent by spawn_id.

        Boundary §9: cancel is cooperative. The scheduler issues the cancel
        command; the actual task may take time to reach CANCELLED state,
        passing through CANCELLING first for non-preemptable operations.
        """
        handle = self._active.get(spawn_id)
        if handle is None:
            logger.warning("subagent.cancel.not_found", spawn_id=spawn_id)
            return

        logger.info("subagent.cancelling", spawn_id=spawn_id)
        handle.status = SubAgentStatus.CANCELLING

        success = await self._scheduler.cancel(spawn_id)
        if success:
            handle.status = SubAgentStatus.CANCELLED
            logger.info("subagent.cancelled", spawn_id=spawn_id)
        else:
            # Task already completed or not found in scheduler
            logger.warning(
                "subagent.cancel.scheduler_miss",
                spawn_id=spawn_id,
                hint="Task may have already completed",
            )

        # Clean up from active set
        self._active.pop(spawn_id, None)

    async def cancel_all(self, parent_run_id: str) -> int:
        """Cancel all active sub-agents for a given parent run."""
        cancelled = 0
        for spawn_id, handle in list(self._active.items()):
            if handle.parent_run_id == parent_run_id:
                await self.cancel(spawn_id)
                cancelled += 1
        logger.info(
            "subagent.cancel_all",
            parent_run_id=parent_run_id,
            cancelled=cancelled,
        )
        return cancelled

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.message import Message
from agent_framework.models.subagent import (AgentProgressTracker,
                                             SpawnMode, SubAgentHandle,
                                             SubAgentResult, SubAgentSpec,
                                             SubAgentStatus,
                                             SubAgentSuspendInfo,
                                             SubAgentSuspendReason,
                                             SubAgentTaskStatus,
                                             TeamContext)
from agent_framework.subagent.factory import SubAgentFactory
from agent_framework.subagent.fork import (
    build_fork_child_messages,
    is_in_fork_child,
)
from agent_framework.subagent.scheduler import SubAgentScheduler

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.agent.coordinator import RunCoordinator
    from agent_framework.agent.runtime_deps import AgentRuntimeDeps
    from agent_framework.models.session import SessionState

logger = get_logger(__name__)


@dataclass
class _LiveAgent:
    """LONG_LIVED agent kept alive between interactions.

    Holds the full runtime context so send_message can resume
    without recreating agent/deps/session.
    """

    agent: Any  # BaseAgent
    deps: Any  # AgentRuntimeDeps
    session_messages: list[Message] = field(default_factory=list)
    handle: SubAgentHandle = field(default_factory=SubAgentHandle)
    parent_agent: Any = None
    last_active: float = field(default_factory=time.monotonic)
    interaction_count: int = 0


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
        live_agent_ttl_seconds: int = 300,
        max_live_agents_per_run: int = 3,
        dynamic_pool: bool = False,
        min_concurrent: int = 1,
        max_concurrent_ceiling: int = 10,
    ) -> None:
        self._factory = SubAgentFactory(parent_deps)
        self._live_agent_ttl = live_agent_ttl_seconds
        self._max_live_agents = max_live_agents_per_run
        self._scheduler = SubAgentScheduler(
            max_concurrent=max_concurrent,
            max_per_run=max_per_run,
            dynamic_pool=dynamic_pool,
            min_concurrent=min_concurrent,
            max_concurrent_ceiling=max_concurrent_ceiling,
        )
        self._coordinator = coordinator
        self._parent_deps = parent_deps
        self._max_spawn_depth = max_spawn_depth
        # LONG_LIVED agent pool — agents kept alive between interactions
        self._live_agents: dict[str, _LiveAgent] = {}
        # active_children truth source — only SubAgentRuntime maintains this
        self._active: dict[str, SubAgentHandle] = {}  # spawn_id -> handle
        # Stream sink: receives child StreamEvents in real-time during spawn/send_message.
        # Signature: (spawn_id, event) -> None. Set by ToolExecutor before execution.
        self._stream_sink: Callable[[str, Any], None] | None = None
        # Checkpoint store for suspend/resume persistence.
        # Set externally (entry.py) when persistent checkpointing is enabled.
        self._checkpoint_store: Any = None  # SQLiteCheckpointStore | None
        # v4.2: Team grouping — agents sharing a team_name
        self._teams: dict[str, TeamContext] = {}
        # v4.2: Auto-background threshold (ms). If a child runs longer than this,
        # mark it as auto-backgrounded in the progress tracker.
        self._auto_background_ms: int = 120_000

    async def _run_child_stream_or_block(
        self,
        coordinator: RunCoordinator,
        agent: Any,
        deps: Any,
        task: str,
        initial_session_messages: list[Message] | None,
        spawn_id: str,
    ) -> Any:
        """Run child via run_stream() if sink is set, else blocking run().

        When _stream_sink is configured, iterates run_stream() and forwards
        all non-terminal events (TOKEN, TOOL_CALL_START, etc.) to the sink
        so they can be displayed in real-time. Returns the AgentRunResult.

        v4.2: Also tracks progress (tool_use_count, tokens, recent_activities)
        and auto-background detection.
        """
        # v4.2: Initialize progress tracker on the handle
        tracker = AgentProgressTracker()
        active = getattr(self, "_active", {})
        handle = active.get(spawn_id)
        if handle is not None:
            handle.progress = tracker

        if self._stream_sink is None:
            start_time = time.monotonic()
            result = await coordinator.run(
                agent, deps, task,
                initial_session_messages=initial_session_messages,
            )
            # Check auto-background after blocking run
            elapsed_ms = (time.monotonic() - start_time) * 1000
            auto_bg_ms = getattr(self, "_auto_background_ms", 120_000)
            if elapsed_ms > auto_bg_ms:
                tracker.auto_backgrounded = True
            return result

        from agent_framework.models.stream import StreamEventType

        start_time = time.monotonic()
        run_result = None
        async for event in coordinator.run_stream(
            agent, deps, task,
            initial_session_messages=initial_session_messages,
        ):
            if event.type == StreamEventType.DONE:
                run_result = event.data.get("result")
            elif event.type == StreamEventType.ERROR:
                # Build a minimal failed result
                from agent_framework.models.agent import AgentRunResult
                from agent_framework.models.message import TokenUsage
                run_result = AgentRunResult(
                    success=False,
                    error=event.data.get("error", "unknown error"),
                    final_answer=None,
                    iterations_used=0,
                    usage=TokenUsage(),
                )
            else:
                # v4.2: Track progress from stream events
                if event.type == StreamEventType.TOOL_CALL_START:
                    tool_name = event.data.get("tool_name", "tool_call")
                    tracker.record_tool_use(tool_name)
                elif event.type == StreamEventType.TOKEN:
                    token_count = event.data.get("token_count", 0)
                    tracker.record_tokens(token_count)
                # v4.2: Auto-background detection
                elapsed_ms = (time.monotonic() - start_time) * 1000
                auto_bg_ms = getattr(self, "_auto_background_ms", 120_000)
                if not tracker.auto_backgrounded and elapsed_ms > auto_bg_ms:
                    tracker.auto_backgrounded = True
                    logger.info(
                        "subagent.auto_backgrounded",
                        spawn_id=spawn_id,
                        elapsed_ms=int(elapsed_ms),
                    )
                self._stream_sink(spawn_id, event)

        if run_result is None:
            from agent_framework.models.agent import AgentRunResult
            from agent_framework.models.message import TokenUsage
            run_result = AgentRunResult(
                success=False,
                error="run_stream yielded no DONE event",
                final_answer=None,
                iterations_used=0,
                usage=TokenUsage(),
            )
        return run_result

    async def spawn(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult:
        """Spawn a sub-agent and wait for its result."""
        self._lazy_evict()
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

        # v4.2: Register in team if team_name is set
        if spec.team_name:
            self._register_team_member(spec.team_name, spec.spawn_id, spec.parent_run_id)

        # FORK mode: build fork-specific context and delegate to spawn_async
        if spec.mode == SpawnMode.FORK:
            return await self._spawn_fork(spec, parent_agent)

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

        # LONG_LIVED: restore session from previous interaction
        is_long_lived = spec.mode == SpawnMode.LONG_LIVED
        if is_long_lived and spec.spawn_id in self._live_agents:
            live = self._live_agents[spec.spawn_id]
            initial_session_messages = list(live.session_messages)
            sub_agent = live.agent
            sub_deps = live.deps
            live.interaction_count += 1
            logger.info(
                "subagent.long_lived.resumed",
                spawn_id=spec.spawn_id,
                interaction=live.interaction_count,
                history_messages=len(initial_session_messages),
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
            run_result = await self._run_child_stream_or_block(
                coordinator, sub_agent, sub_deps,
                spec.task_input, initial_session_messages,
                spawn_id=spec.spawn_id,
            )

            task_record.status = (
                SubAgentTaskStatus.COMPLETED if run_result.success
                else SubAgentTaskStatus.FAILED
            )

            # LONG_LIVED: save session for next interaction
            if is_long_lived:
                saved_messages = list(initial_session_messages)
                saved_messages.append(Message(role="user", content=spec.task_input))
                if run_result.final_answer:
                    saved_messages.append(Message(role="assistant", content=run_result.final_answer))
                self._live_agents[spec.spawn_id] = _LiveAgent(
                    agent=sub_agent,
                    deps=sub_deps,
                    session_messages=saved_messages,
                    handle=handle,
                    parent_agent=parent_agent,
                    last_active=time.monotonic(),
                    interaction_count=getattr(
                        self._live_agents.get(spec.spawn_id, None),
                        "interaction_count", 0,
                    ) + 1,
                )

            logger.info(
                "subagent.run_finished",
                spawn_id=spec.spawn_id,
                child_run_id=child_run_id,
                success=run_result.success,
                iterations_used=run_result.iterations_used,
                total_tokens=run_result.usage.total_tokens,
                long_lived=is_long_lived,
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
            # Remove from active_children after completion
            self._active.pop(spec.spawn_id, None)

        # Handle cancellation status from scheduler
        if handle.status == "CANCELLED":
            task_record.status = SubAgentTaskStatus.CANCELLED

        # LONG_LIVED: set IDLE instead of terminal status
        if is_long_lived and result.success:
            handle.status = SubAgentStatus.IDLE

        logger.info(
            "subagent.spawn_completed",
            spawn_id=spec.spawn_id,
            success=result.success,
            duration_ms=result.duration_ms,
            iterations_used=result.iterations_used,
            task_status=task_record.status.value,
            answer_preview=(result.final_answer or result.error or "")[:120],
            long_lived_idle=is_long_lived and result.success,
        )
        return result

    async def _spawn_fork(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult:
        """Spawn a fork child with parent context for prompt cache sharing.

        Fork children:
        - Inherit parent's last assistant message (with tool_calls)
        - Receive byte-identical placeholder tool_results
        - Get a unique per-child directive
        - Always run asynchronously
        - Cannot spawn further forks (anti-recursion)
        """
        # Anti-recursion guard
        if spec.context_seed and is_in_fork_child(spec.context_seed):
            return SubAgentResult(
                spawn_id=spec.spawn_id,
                success=False,
                error="Fork children cannot spawn further forks (anti-recursion guard)",
                final_status=SubAgentStatus.REJECTED,
            )

        # Get parent's last assistant message with tool_calls
        parent_messages = spec.context_seed or []
        parent_assistant_msg = None
        for msg in reversed(parent_messages):
            if msg.role == "assistant" and msg.tool_calls:
                parent_assistant_msg = msg
                break

        # If no assistant message with tool_calls, build minimal context
        if parent_assistant_msg is None:
            spec.context_seed = [Message(role="user", content=spec.task_input)]
        else:
            fork_messages = build_fork_child_messages(
                parent_assistant_msg, spec.task_input
            )
            # Prepend earlier history before the fork messages
            history_before = []
            for msg in parent_messages:
                if msg is parent_assistant_msg:
                    break
                history_before.append(msg)
            spec.context_seed = history_before + fork_messages

        logger.info(
            "subagent.fork.spawning",
            spawn_id=spec.spawn_id,
            directive_preview=spec.task_input[:100],
            context_messages=len(spec.context_seed) if spec.context_seed else 0,
        )

        # Delegate to the standard spawn path (which handles scheduling, execution, etc.)
        # Remove the FORK branch flag to avoid infinite recursion
        # The context_seed is already set with fork messages
        spec.mode = SpawnMode.EPHEMERAL  # Execute as ephemeral once context is built
        return await self.spawn(spec, parent_agent)

    # ------------------------------------------------------------------
    # v4.2: Team management
    # ------------------------------------------------------------------

    def _register_team_member(
        self, team_name: str, spawn_id: str, parent_run_id: str
    ) -> None:
        """Register an agent as a member of a named team."""
        if team_name in self._teams:
            # Add to existing team (TeamContext is frozen, rebuild)
            old = self._teams[team_name]
            self._teams[team_name] = TeamContext(
                team_name=team_name,
                leader_spawn_id=old.leader_spawn_id,
                member_spawn_ids=list(old.member_spawn_ids) + [spawn_id],
            )
        else:
            # First member becomes leader
            self._teams[team_name] = TeamContext(
                team_name=team_name,
                leader_spawn_id=spawn_id,
                member_spawn_ids=[spawn_id],
            )
        logger.info(
            "subagent.team.registered",
            team_name=team_name,
            spawn_id=spawn_id,
            team_size=len(self._teams[team_name].member_spawn_ids),
        )

    def get_team_members(self, team_name: str) -> list[dict]:
        """Get all members of a named team with their status and progress."""
        team = self._teams.get(team_name)
        if team is None:
            return []
        members = []
        for sid in team.member_spawn_ids:
            handle = self._active.get(sid)
            member_info: dict = {
                "spawn_id": sid,
                "is_leader": sid == team.leader_spawn_id,
            }
            if handle is not None:
                member_info["status"] = handle.status.value if hasattr(handle.status, "value") else str(handle.status)
                if handle.progress is not None:
                    member_info["progress"] = {
                        "tool_use_count": handle.progress.tool_use_count,
                        "cumulative_output_tokens": handle.progress.cumulative_output_tokens,
                        "recent_activities": handle.progress.recent_activities,
                        "auto_backgrounded": handle.progress.auto_backgrounded,
                    }
            else:
                member_info["status"] = "UNKNOWN"
            members.append(member_info)
        return members

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

    def save_checkpoint(
        self, spawn_id: str, agent_state: Any, session_state: Any,
        summary: str = "",
        trigger: str = "user_input",
    ) -> str | None:
        """Save a checkpoint for a running sub-agent.

        Only allowed at real user interaction boundaries (trigger="user_input").
        Returns checkpoint_id on success, None if no checkpoint store configured.
        """
        if self._checkpoint_store is None:
            logger.debug("checkpoint.no_store", spawn_id=spawn_id)
            return None

        from agent_framework.models.subagent import CheckpointLevel
        checkpoint_id = self._checkpoint_store.save(
            spawn_id=spawn_id,
            agent_state=agent_state,
            session_state=session_state,
            checkpoint_level=CheckpointLevel.STEP_RESUMABLE,
            summary=summary,
            trigger=trigger,
        )
        return checkpoint_id

    async def resume_from_checkpoint(
        self, spawn_id: str, parent_agent: Any,
        checkpoint_id: str | None = None,
    ) -> SubAgentResult:
        """Resume a sub-agent from a stored checkpoint.

        If checkpoint_id is None, uses the latest checkpoint for spawn_id.
        Restores AgentState + SessionState and continues execution.
        """
        if self._checkpoint_store is None:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error="No checkpoint store configured",
            )

        # Load checkpoint
        if checkpoint_id:
            ckpt = self._checkpoint_store.load_by_id(checkpoint_id)
        else:
            ckpt = self._checkpoint_store.load_latest(spawn_id)

        if ckpt is None:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error=f"No checkpoint found for spawn_id={spawn_id}",
            )

        logger.info(
            "subagent.resume_from_checkpoint",
            spawn_id=spawn_id,
            checkpoint_id=ckpt.checkpoint_id,
            iteration_index=ckpt.iteration_index,
            level=ckpt.checkpoint_level.value,
        )

        # Restore state
        restored_agent_state = ckpt.restore_agent_state()
        restored_session = ckpt.restore_session_state()

        # Create agent + deps from factory
        spec = SubAgentSpec(
            parent_run_id=restored_agent_state.run_id,
            spawn_id=spawn_id,
            task_input=restored_agent_state.task,
        )
        try:
            sub_agent, sub_deps = self._factory.create_agent_and_deps(
                spec, parent_agent,
            )
        except Exception as e:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error=f"Failed to create agent for checkpoint resume: {e}",
            )

        coordinator = self._coordinator
        if coordinator is None:
            from agent_framework.agent.coordinator import RunCoordinator
            coordinator = RunCoordinator()

        # Run with restored session as initial context
        try:
            run_result = await self._run_child_stream_or_block(
                coordinator, sub_agent, sub_deps,
                restored_agent_state.task,
                restored_session.get_messages(),
                spawn_id=spawn_id,
            )
            return SubAgentResult(
                spawn_id=spawn_id,
                success=run_result.success,
                final_answer=run_result.final_answer,
                error=run_result.error,
                usage=run_result.usage,
                iterations_used=run_result.iterations_used,
            )
        except Exception as e:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error=f"Checkpoint resume execution failed: {e}",
            )

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

    # ------------------------------------------------------------------
    # LONG_LIVED: send_message / close / cleanup
    # ------------------------------------------------------------------

    async def send_message(
        self, spawn_id: str, message: str, parent_agent: Any = None,
    ) -> SubAgentResult:
        """Send a message to a LONG_LIVED agent in IDLE state.

        The agent wakes up with the full prior conversation + new message,
        runs to completion, then returns to IDLE.
        """
        self._lazy_evict()
        live = self._live_agents.get(spawn_id)
        if live is None:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error=f"No LONG_LIVED agent with spawn_id={spawn_id}. "
                      "Use spawn_agent(mode='LONG_LIVED') first.",
            )

        live.last_active = time.monotonic()

        logger.info(
            "subagent.send_message",
            spawn_id=spawn_id,
            interaction=live.interaction_count + 1,
            history_messages=len(live.session_messages),
            message_preview=message[:100],
        )

        # Build spec to reuse spawn() with the existing session
        spec = SubAgentSpec(
            parent_run_id=live.handle.parent_run_id,
            spawn_id=spawn_id,
            mode=SpawnMode.LONG_LIVED,
            task_input=message,
            max_iterations=10,
        )

        # Temporarily re-register in _active for the run
        live.handle.status = SubAgentStatus.RUNNING
        self._active[spawn_id] = live.handle

        coordinator = self._coordinator
        if coordinator is None:
            from agent_framework.agent.coordinator import RunCoordinator
            coordinator = RunCoordinator()

        try:
            run_result = await self._run_child_stream_or_block(
                coordinator, live.agent, live.deps,
                message, list(live.session_messages),
                spawn_id=spawn_id,
            )

            # Update session: append this exchange
            live.session_messages.append(Message(role="user", content=message))
            if run_result.final_answer:
                live.session_messages.append(
                    Message(role="assistant", content=run_result.final_answer)
                )
            live.interaction_count += 1
            live.last_active = time.monotonic()
            live.handle.status = SubAgentStatus.IDLE

            logger.info(
                "subagent.send_message.completed",
                spawn_id=spawn_id,
                success=run_result.success,
                interaction=live.interaction_count,
                total_messages=len(live.session_messages),
            )

            return SubAgentResult(
                spawn_id=spawn_id,
                success=run_result.success,
                final_answer=run_result.final_answer,
                error=run_result.error,
                usage=run_result.usage,
                iterations_used=run_result.iterations_used,
            )
        except Exception as e:
            live.handle.status = SubAgentStatus.IDLE  # Return to IDLE even on error
            logger.error(
                "subagent.send_message.failed",
                spawn_id=spawn_id, error=str(e),
            )
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error=f"send_message failed: {e}",
            )
        finally:
            self._active.pop(spawn_id, None)

    def close_live_agent(self, spawn_id: str) -> bool:
        """Explicitly close a LONG_LIVED agent, releasing all resources."""
        live = self._live_agents.pop(spawn_id, None)
        if live is None:
            return False
        logger.info(
            "subagent.long_lived.closed",
            spawn_id=spawn_id,
            interactions=live.interaction_count,
            messages=len(live.session_messages),
        )
        return True

    def cleanup_live_agents(self, parent_run_id: str | None = None) -> int:
        """Clean up IDLE LONG_LIVED agents. Called when parent run ends.

        If parent_run_id is specified, only clean agents under that run.
        Otherwise clean all.
        """
        to_remove = []
        for spawn_id, live in self._live_agents.items():
            if parent_run_id is None or live.handle.parent_run_id == parent_run_id:
                to_remove.append(spawn_id)

        for spawn_id in to_remove:
            self._live_agents.pop(spawn_id, None)

        if to_remove:
            logger.info(
                "subagent.long_lived.cleanup",
                parent_run_id=parent_run_id,
                cleaned=len(to_remove),
            )
        return len(to_remove)

    def _lazy_evict(self) -> None:
        """Lazily evict expired LONG_LIVED agents on access.

        Called automatically before spawn() and send_message() so that
        stale agents are cleaned up without needing a background timer.
        Only runs when there are live agents to check.
        """
        if self._live_agents:
            self.evict_expired_live_agents()

    def evict_expired_live_agents(self) -> int:
        """Evict LONG_LIVED agents that exceeded TTL. Called during drain."""
        now = time.monotonic()
        expired = [
            sid for sid, live in self._live_agents.items()
            if (now - live.last_active) > self._live_agent_ttl
        ]
        for sid in expired:
            self._live_agents.pop(sid, None)
        if expired:
            logger.info(
                "subagent.long_lived.evicted",
                count=len(expired),
                ttl_seconds=self._live_agent_ttl,
            )
        return len(expired)

    def get_live_agent_status(self, spawn_id: str) -> dict | None:
        """Get status of a LONG_LIVED agent."""
        live = self._live_agents.get(spawn_id)
        if live is None:
            return None
        return {
            "spawn_id": spawn_id,
            "status": live.handle.status.value if hasattr(live.handle.status, "value") else str(live.handle.status),
            "interaction_count": live.interaction_count,
            "session_messages": len(live.session_messages),
            "idle_seconds": int(time.monotonic() - live.last_active),
        }

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.hook import HookContext, HookPoint
from agent_framework.hooks.errors import HookDeniedError
from agent_framework.models.subagent import (
    ArtifactRef,
    DelegationErrorCode,
    DelegationSummary,
    SubAgentResult,
    SubAgentSpec,
    SubAgentStatus,
    resolve_delegation_status,
)

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.protocols.core import SubAgentRuntimeProtocol

logger = get_logger(__name__)


class DelegationExecutor:
    """Handles delegation to sub-agents and A2A agents.

    Doc 16.2 spawn flow:
    1. allow_spawn check
    2. quota check
    3. build spawn seed
    4. SubAgentFactory.create()
    5. SubAgentScheduler.submit()
    6. await_result()
    7. DelegationSummary -> ToolResult.output
    """

    def __init__(
        self,
        sub_agent_runtime: SubAgentRuntimeProtocol | None = None,
        hook_executor: Any = None,
    ) -> None:
        self._sub_agent_runtime = sub_agent_runtime
        self._a2a_adapter: Any = None
        self._hook_executor = hook_executor

    async def delegate_to_subagent(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult:
        """Delegate task to a sub-agent with permission checks (doc 16.2/20.2)."""
        parent_id = getattr(parent_agent, "agent_id", "unknown") if parent_agent else "none"

        logger.info(
            "delegation.subagent.requested",
            parent_agent_id=parent_id,
            task_input=spec.task_input[:150],
            mode=spec.mode.value if hasattr(spec.mode, "value") else str(spec.mode),
            memory_scope=spec.memory_scope.value if hasattr(spec.memory_scope, "value") else str(spec.memory_scope),
        )

        # Check: SubAgentRuntime must be configured
        if self._sub_agent_runtime is None:
            logger.error("delegation.subagent.no_runtime", parent_agent_id=parent_id)
            return SubAgentResult(
                spawn_id=spec.spawn_id,
                success=False,
                error="SubAgentRuntime not configured",
            )

        # BaseAgent hook check (returns SpawnDecision)
        if parent_agent is not None and hasattr(parent_agent, "on_spawn_requested"):
            spawn_decision = await parent_agent.on_spawn_requested(spec)
            if not spawn_decision.allowed:
                logger.warning(
                    "delegation.subagent.hook_denied",
                    parent_agent_id=parent_id,
                    reason=spawn_decision.reason or "on_spawn_requested denied",
                )
                return SubAgentResult(
                    spawn_id=spec.spawn_id,
                    success=False,
                    error=f"PERMISSION_DENIED: {spawn_decision.reason or 'spawn rejected by parent agent hook'}",
                )

        # Doc 16.2 step 1: allow_spawn check
        if parent_agent is not None:
            config = getattr(parent_agent, "agent_config", None)
            if config is not None:
                # Doc 20.2: sub-agents have allow_spawn_children=False,
                # so they will be blocked here
                if not getattr(config, "allow_spawn_children", True):
                    logger.warning(
                        "delegation.subagent.spawn_denied",
                        agent_id=parent_id,
                        reason="allow_spawn_children=False",
                        hint="Sub-agents cannot spawn children (recursive spawn protection)",
                    )
                    return SubAgentResult(
                        spawn_id=spec.spawn_id,
                        success=False,
                        error="PERMISSION_DENIED: This agent is not allowed to spawn children",
                    )

        logger.info(
            "delegation.subagent.approved",
            parent_agent_id=parent_id,
            task_input=spec.task_input[:80],
        )

        # PRE_DELEGATION hook
        if self._hook_executor is not None:
            try:
                await self._hook_executor.execute_chain(
                    HookPoint.PRE_DELEGATION,
                    HookContext(
                        run_id=spec.parent_run_id,
                        payload={
                            "task_input": spec.task_input[:500],
                            "mode": str(spec.mode.value),
                            "memory_scope": str(spec.memory_scope.value),
                        },
                    ),
                )
            except HookDeniedError as hde:
                return SubAgentResult(
                    spawn_id=spec.spawn_id,
                    success=False,
                    error=f"PERMISSION_DENIED: Hook denied delegation: {hde}",
                )

        result = await self._sub_agent_runtime.spawn(spec, parent_agent)

        # POST_DELEGATION hook
        if self._hook_executor is not None:
            try:
                await self._hook_executor.execute_chain(
                    HookPoint.POST_DELEGATION,
                    HookContext(
                        run_id=spec.parent_run_id,
                        payload={
                            "spawn_id": result.spawn_id,
                            "success": result.success,
                            "iterations_used": result.iterations_used,
                        },
                    ),
                )
            except Exception:
                pass

        return result

    async def delegate_to_subagent_async(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> str:
        """Start a sub-agent asynchronously. Returns spawn_id without waiting.

        Same permission checks as delegate_to_subagent, but calls
        SubAgentRuntime.spawn_async() which submits without blocking.
        Use collect_subagent_result() to get the result later.
        """
        parent_id = getattr(parent_agent, "agent_id", "unknown") if parent_agent else "none"

        if self._sub_agent_runtime is None:
            raise RuntimeError("SubAgentRuntime not configured")

        # Same permission checks as synchronous path
        if parent_agent is not None and hasattr(parent_agent, "on_spawn_requested"):
            spawn_decision = await parent_agent.on_spawn_requested(spec)
            if not spawn_decision.allowed:
                raise RuntimeError(
                    f"PERMISSION_DENIED: {spawn_decision.reason or 'spawn rejected by parent agent hook'}"
                )

        if parent_agent is not None:
            config = getattr(parent_agent, "agent_config", None)
            if config is not None and not getattr(config, "allow_spawn_children", True):
                raise RuntimeError("PERMISSION_DENIED: This agent is not allowed to spawn children")

        logger.info(
            "delegation.subagent.async_approved",
            parent_agent_id=parent_id,
            task_input=spec.task_input[:80],
        )
        return await self._sub_agent_runtime.spawn_async(spec, parent_agent)

    async def collect_subagent_result(
        self, spawn_id: str, wait: bool = True
    ) -> SubAgentResult | None:
        """Collect result of an async sub-agent.

        Args:
            spawn_id: The spawn_id returned by delegate_to_subagent_async.
            wait: If True, block until complete. If False, return None if still running.

        Returns:
            SubAgentResult if complete, None if still running (wait=False).
        """
        if self._sub_agent_runtime is None:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error="SubAgentRuntime not configured",
            )
        return await self._sub_agent_runtime.collect_result(spawn_id, wait=wait)

    def set_a2a_adapter(self, adapter: Any) -> None:
        """Wire the A2A client adapter for delegation."""
        self._a2a_adapter = adapter

    async def delegate_to_a2a(
        self,
        agent_url: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> SubAgentResult:
        """Delegate to a remote A2A agent.

        All failure modes are mapped to unified DelegationErrorCode so the
        main agent loop sees the same error vocabulary as local subagent failures.
        """
        if self._a2a_adapter is None:
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"{DelegationErrorCode.REMOTE_UNAVAILABLE}: A2A adapter not configured",
            )

        # Find alias by URL — use public API, not private _known_agents
        alias = self._a2a_adapter.resolve_alias(agent_url)

        if alias is None:
            # Try to discover on the fly
            try:
                await self._a2a_adapter.discover_agent(agent_url)
                alias = self._a2a_adapter.resolve_alias(agent_url)
            except Exception as e:
                logger.warning(
                    "delegation.a2a.discovery_failed",
                    agent_url=agent_url,
                    error=str(e),
                )

        if alias is None:
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"{DelegationErrorCode.REMOTE_UNAVAILABLE}: A2A agent at {agent_url} not discoverable",
            )

        try:
            result = await self._a2a_adapter.delegate_task(alias, task_input, skill_id)
            # POST_DELEGATION hook for A2A
            if self._hook_executor is not None:
                try:
                    await self._hook_executor.execute_chain(
                        HookPoint.POST_DELEGATION,
                        HookContext(
                            payload={
                                "spawn_id": result.spawn_id,
                                "success": result.success,
                                "agent_url": agent_url,
                            },
                        ),
                    )
                except Exception:
                    pass
            return result
        except TimeoutError:
            # DELEGATION_ERROR hook
            if self._hook_executor is not None:
                try:
                    await self._hook_executor.execute_chain(
                        HookPoint.DELEGATION_ERROR,
                        HookContext(
                            payload={"agent_url": agent_url, "error": "timeout"},
                        ),
                    )
                except Exception:
                    pass
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"{DelegationErrorCode.TIMEOUT}: A2A delegation timed out for {agent_url}",
            )
        except Exception as e:
            # DELEGATION_ERROR hook
            if self._hook_executor is not None:
                try:
                    await self._hook_executor.execute_chain(
                        HookPoint.DELEGATION_ERROR,
                        HookContext(
                            payload={"agent_url": agent_url, "error": str(e)},
                        ),
                    )
                except Exception:
                    pass
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"{DelegationErrorCode.DELEGATION_FAILED}: {e}",
            )

    @staticmethod
    def summarize_result(result: SubAgentResult) -> DelegationSummary:
        """Convert a SubAgentResult to a DelegationSummary for LLM consumption.

        Error codes are unified across local subagent and remote A2A delegation
        via DelegationErrorCode enum, so the main agent loop sees a consistent
        error vocabulary regardless of delegation target.

        v2.6.4 §44: Uses unified SubAgentStatus state machine. Status is
        resolved via resolve_delegation_status() before building summary.
        Local subagent and A2A paths use the same status enum.
        """
        summary_text = result.final_answer or result.error or ""
        # Add termination hint so the model knows to stop calling spawn_agent
        if result.success:
            summary_text += (
                "\n\n[Sub-agent task completed successfully. "
                "Summarize this result for the user. Do NOT call spawn_agent again.]"
            )

        # Classify error code from error message
        error_code: str | None = None
        if not result.success:
            error_code = DelegationExecutor._classify_error_code(result.error)

        # Resolve unified status (v2.6.4 §44)
        delegation_status = resolve_delegation_status(result, error_code)

        return DelegationSummary(
            status=delegation_status.value,
            summary=summary_text,
            artifacts_digest=[a.name for a in result.artifacts],
            artifact_refs=[
                ArtifactRef(
                    name=a.name,
                    artifact_type=a.artifact_type,
                    uri=a.uri,
                )
                for a in result.artifacts
            ],
            error_code=error_code,
        )

    @staticmethod
    def _classify_error_code(error: str | None) -> str:
        """Map error messages to unified DelegationErrorCode.

        Both local subagent and A2A failures are classified into the same
        vocabulary so the main loop can handle them uniformly.
        """
        if not error:
            return DelegationErrorCode.DELEGATION_FAILED

        error_upper = error.upper()
        if "TIMEOUT" in error_upper or "TIMED OUT" in error_upper:
            return DelegationErrorCode.TIMEOUT
        if "QUOTA" in error_upper:
            return DelegationErrorCode.QUOTA_EXCEEDED
        if "PERMISSION_DENIED" in error_upper:
            return DelegationErrorCode.PERMISSION_DENIED
        if "UNAVAILABLE" in error_upper or "NOT CONFIGURED" in error_upper:
            return DelegationErrorCode.REMOTE_UNAVAILABLE
        return DelegationErrorCode.DELEGATION_FAILED

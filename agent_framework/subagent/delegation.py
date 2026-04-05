"""DelegationExecutor — unified delegation to sub-agents and A2A agents.

v3.1: Extended with resume/cancel, interaction channel event emission,
and HITL request forwarding for long-term parent-child interaction.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from agent_framework.hooks.dispatcher import HookDispatchService
from agent_framework.hooks.errors import HookDeniedError
from agent_framework.hooks.payloads import (delegation_error_payload,
                                            delegation_post_payload)
from agent_framework.infra.logger import get_logger
from agent_framework.models.hook import HookPoint
from agent_framework.models.subagent import (ArtifactRef, DelegationErrorCode,
                                             DelegationEventType,
                                             DelegationMode, DelegationSummary,
                                             HITLRequest, HITLResponse,
                                             SubAgentResult, SubAgentSpec,
                                             SubAgentStatus,
                                             SubAgentSuspendInfo,
                                             SubAgentSuspendReason,
                                             resolve_delegation_status)
from agent_framework.subagent.delegation_hooks import (
    _DelegationConfirmationDenied, apply_pre_delegation_hooks)

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.protocols.core import SubAgentRuntimeProtocol
    from agent_framework.subagent.interaction_channel import \
        InMemoryInteractionChannel

logger = get_logger(__name__)


class DelegationExecutor:
    """Handles delegation to sub-agents and A2A agents.

    v3.1 spawn flow (extended for long-term interaction):
    1. allow_spawn check
    2. quota check
    3. PRE_DELEGATION hooks
    4. SubAgentRuntime.spawn() / spawn_async()
    5. [INTERACTIVE] Event exchange via InteractionChannel
    6. [WAITING] HITL request forwarding
    7. [SUSPEND/RESUME] resume_subagent()
    8. POST_DELEGATION / DELEGATION_ERROR hooks
    9. DelegationSummary -> ToolResult.output
    """

    def __init__(
        self,
        sub_agent_runtime: SubAgentRuntimeProtocol | None = None,
        hook_executor: Any = None,
        confirmation_handler: Any = None,
        interaction_channel: InMemoryInteractionChannel | None = None,
        hitl_handler: Any = None,
    ) -> None:
        self._sub_agent_runtime = sub_agent_runtime
        self._a2a_adapter: Any = None
        self._hook_executor = hook_executor
        self._hook_dispatcher: HookDispatchService | None = (
            HookDispatchService(hook_executor) if hook_executor is not None else None
        )
        self._confirmation = confirmation_handler
        self._interaction_channel = interaction_channel
        self._hitl_handler = hitl_handler

    def set_interaction_channel(self, channel: InMemoryInteractionChannel) -> None:
        """Wire the interaction channel (may be set after construction)."""
        self._interaction_channel = channel

    def set_hitl_handler(self, handler: Any) -> None:
        """Wire the HITL handler (may be set after construction)."""
        self._hitl_handler = handler

    # ------------------------------------------------------------------
    # Permission checks (shared by sync/async/resume paths)
    # ------------------------------------------------------------------

    async def _check_spawn_permissions(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult | None:
        """Run permission checks. Returns error result if denied, None if OK."""
        parent_id = _parent_id(parent_agent)

        if self._sub_agent_runtime is None:
            logger.error("delegation.subagent.no_runtime", parent_agent_id=parent_id)
            return SubAgentResult(
                spawn_id=spec.spawn_id, success=False,
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
                    spawn_id=spec.spawn_id, success=False,
                    error=f"PERMISSION_DENIED: {spawn_decision.reason or 'spawn rejected by parent agent hook'}",
                )

        # allow_spawn_children check
        if parent_agent is not None:
            config = getattr(parent_agent, "agent_config", None)
            if config is not None and not getattr(config, "allow_spawn_children", True):
                logger.warning(
                    "delegation.subagent.spawn_denied",
                    agent_id=parent_id,
                    reason="allow_spawn_children=False",
                )
                return SubAgentResult(
                    spawn_id=spec.spawn_id, success=False,
                    error="PERMISSION_DENIED: This agent is not allowed to spawn children",
                )

        return None  # OK

    async def _apply_pre_hooks(self, spec: SubAgentSpec, is_async: bool = False) -> SubAgentSpec:
        """Apply PRE_DELEGATION hooks. May raise HookDeniedError or _DelegationConfirmationDenied."""
        if self._hook_dispatcher is not None:
            spec = await apply_pre_delegation_hooks(
                self._hook_dispatcher, spec,
                is_async=is_async,
                confirmation_handler=self._confirmation,
            )
        return spec

    # ------------------------------------------------------------------
    # Event emission helpers
    # ------------------------------------------------------------------

    def _emit_event(
        self,
        spawn_id: str,
        parent_run_id: str,
        event_type: DelegationEventType,
        payload: dict | None = None,
        requires_ack: bool = False,
    ) -> None:
        """Emit a delegation event to the interaction channel (if wired)."""
        if self._interaction_channel is not None:
            self._interaction_channel.emit_event(
                spawn_id=spawn_id,
                parent_run_id=parent_run_id,
                event_type=event_type,
                payload=payload or {},
                requires_ack=requires_ack,
            )

    # ------------------------------------------------------------------
    # Synchronous delegation
    # ------------------------------------------------------------------

    async def delegate_to_subagent(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> SubAgentResult:
        """Delegate task to a sub-agent with permission checks."""
        parent_id = _parent_id(parent_agent)

        logger.info(
            "delegation.subagent.requested",
            parent_agent_id=parent_id,
            task_input=spec.task_input[:150],
            mode=spec.mode.value,
            delegation_mode=spec.delegation_mode.value,
            memory_scope=spec.memory_scope.value,
        )

        # Permission checks
        denied = await self._check_spawn_permissions(spec, parent_agent)
        if denied is not None:
            return denied

        logger.info(
            "delegation.subagent.approved",
            parent_agent_id=parent_id,
            task_input=spec.task_input[:80],
        )

        # PRE_DELEGATION hooks
        try:
            spec = await self._apply_pre_hooks(spec)
        except HookDeniedError as hde:
            return SubAgentResult(
                spawn_id=spec.spawn_id, success=False,
                error=f"PERMISSION_DENIED: Hook denied delegation: {hde}",
            )
        except _DelegationConfirmationDenied:
            return SubAgentResult(
                spawn_id=spec.spawn_id, success=False,
                error="PERMISSION_DENIED: User denied delegation confirmation",
            )

        # Emit STARTED event
        self._emit_event(spec.spawn_id, spec.parent_run_id, DelegationEventType.STARTED, {
            "task_input": spec.task_input[:200],
            "delegation_mode": spec.delegation_mode.value,
        })

        # Spawn
        assert self._sub_agent_runtime is not None
        result = await self._sub_agent_runtime.spawn(spec, parent_agent)

        # Emit completion event
        if result.success:
            self._emit_event(spec.spawn_id, spec.parent_run_id, DelegationEventType.COMPLETED, {
                "summary": (result.final_answer or "")[:500],
            })
        elif result.suspend_info is not None:
            self._emit_event(spec.spawn_id, spec.parent_run_id, DelegationEventType.SUSPENDED, {
                "reason": result.suspend_info.reason.value,
                "message": result.suspend_info.message,
            })
        else:
            self._emit_event(spec.spawn_id, spec.parent_run_id, DelegationEventType.FAILED, {
                "error": (result.error or "")[:500],
            })

        # POST hooks
        if self._hook_dispatcher is not None:
            await self._hook_dispatcher.fire_advisory(
                HookPoint.POST_DELEGATION,
                run_id=spec.parent_run_id,
                payload=delegation_post_payload(
                    result.spawn_id, result.success, result.iterations_used,
                ),
            )

        return result

    # ------------------------------------------------------------------
    # Asynchronous (non-blocking) delegation
    # ------------------------------------------------------------------

    async def delegate_to_subagent_async(
        self, spec: SubAgentSpec, parent_agent: Any
    ) -> str:
        """Start a sub-agent asynchronously. Returns spawn_id without waiting."""
        parent_id = _parent_id(parent_agent)

        if self._sub_agent_runtime is None:
            raise RuntimeError("SubAgentRuntime not configured")

        # Permission checks (raise on denial for async path)
        denied = await self._check_spawn_permissions(spec, parent_agent)
        if denied is not None:
            raise RuntimeError(denied.error or "Delegation denied")

        # PRE_DELEGATION hooks
        try:
            spec = await self._apply_pre_hooks(spec, is_async=True)
        except HookDeniedError as hde:
            raise RuntimeError(f"PERMISSION_DENIED: Hook denied delegation: {hde}")
        except _DelegationConfirmationDenied:
            raise RuntimeError("PERMISSION_DENIED: User denied delegation confirmation")

        logger.info(
            "delegation.subagent.async_approved",
            parent_agent_id=parent_id,
            task_input=spec.task_input[:80],
        )

        # Emit STARTED event
        self._emit_event(spec.spawn_id, spec.parent_run_id, DelegationEventType.STARTED, {
            "task_input": spec.task_input[:200],
            "delegation_mode": spec.delegation_mode.value,
            "async": True,
        })

        return await self._sub_agent_runtime.spawn_async(spec, parent_agent)

    # ------------------------------------------------------------------
    # Collect async result
    # ------------------------------------------------------------------

    async def collect_subagent_result(
        self, spawn_id: str, wait: bool = True
    ) -> SubAgentResult | None:
        """Collect result of an async sub-agent. Fires POST hooks."""
        if self._sub_agent_runtime is None:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error="SubAgentRuntime not configured",
            )
        result = await self._sub_agent_runtime.collect_result(spawn_id, wait=wait)

        if result is not None:
            # Emit completion/failure event
            if result.success:
                self._emit_event(spawn_id, "", DelegationEventType.COMPLETED, {
                    "summary": (result.final_answer or "")[:500],
                })
            elif result.suspend_info is not None:
                self._emit_event(spawn_id, "", DelegationEventType.SUSPENDED, {
                    "reason": result.suspend_info.reason.value,
                    "message": result.suspend_info.message,
                })
            else:
                self._emit_event(spawn_id, "", DelegationEventType.FAILED, {
                    "error": (result.error or "")[:500],
                })

            if self._hook_dispatcher is not None:
                if result.success:
                    await self._hook_dispatcher.fire_advisory(
                        HookPoint.POST_DELEGATION,
                        payload=delegation_post_payload(
                            result.spawn_id, True, result.iterations_used,
                            async_collected=True,
                        ),
                    )
                else:
                    await self._hook_dispatcher.fire_advisory(
                        HookPoint.DELEGATION_ERROR,
                        payload=delegation_error_payload(
                            result.spawn_id, result.error or "",
                            async_collected=True,
                        ),
                    )

        return result

    # ------------------------------------------------------------------
    # Resume (v3.1 long-term interaction)
    # ------------------------------------------------------------------

    def save_checkpoint(
        self,
        spawn_id: str,
        agent_state: Any,
        session_state: Any,
        summary: str = "",
    ) -> str | None:
        """Save a checkpoint at a user interaction boundary.

        Only call this when a real user has provided input (HITL response,
        send_message, etc.). Automated saves are rejected by the store.
        """
        if self._sub_agent_runtime is None:
            return None
        return self._sub_agent_runtime.save_checkpoint(
            spawn_id, agent_state, session_state,
            summary=summary, trigger="user_input",
        )

    async def resume_from_checkpoint(
        self,
        spawn_id: str,
        parent_agent: Any,
        checkpoint_id: str | None = None,
    ) -> SubAgentResult:
        """Resume a sub-agent from a stored checkpoint.

        Restores the exact AgentState + SessionState from the checkpoint
        and continues execution from that point.
        """
        if self._sub_agent_runtime is None:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error="SubAgentRuntime not configured",
            )

        logger.info(
            "delegation.resume_from_checkpoint",
            spawn_id=spawn_id,
            checkpoint_id=checkpoint_id or "latest",
        )

        self._emit_event(spawn_id, "", DelegationEventType.RESUMED, {
            "source": "checkpoint",
            "checkpoint_id": checkpoint_id or "latest",
        })

        return await self._sub_agent_runtime.resume_from_checkpoint(
            spawn_id, parent_agent, checkpoint_id=checkpoint_id,
        )

    async def resume_subagent(
        self,
        spawn_id: str,
        resume_payload: dict,
        parent_agent: Any,
    ) -> SubAgentResult:
        """Resume a suspended/waiting sub-agent with additional input.

        The resume_payload is forwarded to the sub-agent runtime, which
        injects it as a user message or restores from checkpoint.
        """
        if self._sub_agent_runtime is None:
            return SubAgentResult(
                spawn_id=spawn_id, success=False,
                error="SubAgentRuntime not configured",
            )

        parent_id = _parent_id(parent_agent)
        logger.info(
            "delegation.subagent.resume",
            parent_agent_id=parent_id,
            spawn_id=spawn_id,
        )

        # Emit RESUMED event
        self._emit_event(spawn_id, "", DelegationEventType.RESUMED, {
            "resume_payload_keys": list(resume_payload.keys()),
        })

        result = await self._sub_agent_runtime.resume(
            spawn_id, resume_payload, parent_agent
        )

        # Emit completion event
        if result.success:
            self._emit_event(spawn_id, "", DelegationEventType.COMPLETED, {
                "summary": (result.final_answer or "")[:500],
            })
        elif result.suspend_info is not None:
            self._emit_event(spawn_id, "", DelegationEventType.SUSPENDED, {
                "reason": result.suspend_info.reason.value,
                "message": result.suspend_info.message,
            })
        else:
            self._emit_event(spawn_id, "", DelegationEventType.FAILED, {
                "error": (result.error or "")[:500],
            })

        # POST hooks
        if self._hook_dispatcher is not None:
            await self._hook_dispatcher.fire_advisory(
                HookPoint.POST_DELEGATION,
                run_id="",
                payload=delegation_post_payload(
                    result.spawn_id, result.success, result.iterations_used,
                ),
            )

        return result

    # ------------------------------------------------------------------
    # Cancel (v3.1)
    # ------------------------------------------------------------------

    async def cancel_subagent(self, spawn_id: str) -> None:
        """Cancel a running/waiting/suspended sub-agent."""
        if self._sub_agent_runtime is None:
            raise RuntimeError("SubAgentRuntime not configured")

        logger.info("delegation.subagent.cancel", spawn_id=spawn_id)

        self._emit_event(spawn_id, "", DelegationEventType.CANCELLED)

        await self._sub_agent_runtime.cancel(spawn_id)

    # ------------------------------------------------------------------
    # HITL forwarding (v3.1 PRD §9)
    # ------------------------------------------------------------------

    async def forward_hitl_request(
        self, request: HITLRequest
    ) -> HITLResponse | None:
        """Forward a HITL request from a sub-agent to the user via parent chain.

        Flow: sub-agent event → DelegationExecutor → HITLHandler → user → response
        Returns None if no HITL handler is configured.
        """
        if self._hitl_handler is None:
            logger.warning(
                "delegation.hitl.no_handler",
                request_id=request.request_id,
                spawn_id=request.spawn_id,
            )
            return None

        logger.info(
            "delegation.hitl.forwarding",
            request_id=request.request_id,
            spawn_id=request.spawn_id,
            request_type=request.request_type,
        )

        response = await self._hitl_handler.handle_hitl_request(request)

        logger.info(
            "delegation.hitl.response_received",
            request_id=request.request_id,
            response_type=response.response_type,
        )

        return response

    # ------------------------------------------------------------------
    # A2A delegation
    # ------------------------------------------------------------------

    def set_a2a_adapter(self, adapter: Any) -> None:
        """Wire the A2A client adapter for delegation."""
        self._a2a_adapter = adapter

    async def delegate_to_a2a(
        self,
        agent_url: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> SubAgentResult:
        """Delegate to a remote A2A agent."""
        if self._a2a_adapter is None:
            return SubAgentResult(
                spawn_id="", success=False,
                error=f"{DelegationErrorCode.REMOTE_UNAVAILABLE}: A2A adapter not configured",
            )

        alias = self._a2a_adapter.resolve_alias(agent_url)

        if alias is None:
            try:
                await self._a2a_adapter.discover_agent(agent_url)
                alias = self._a2a_adapter.resolve_alias(agent_url)
            except Exception as e:
                logger.warning(
                    "delegation.a2a.discovery_failed",
                    agent_url=agent_url, error=str(e),
                )

        if alias is None:
            return SubAgentResult(
                spawn_id="", success=False,
                error=f"{DelegationErrorCode.REMOTE_UNAVAILABLE}: A2A agent at {agent_url} not discoverable",
            )

        try:
            # Query discovered capabilities (boundary §10)
            caps = self._a2a_adapter.get_capabilities(alias)
            logger.info(
                "delegation.a2a.capabilities",
                alias=alias,
                supports_progress=caps.supports_progress_events,
                supports_suspend_resume=caps.supports_suspend_resume,
                checkpoint_level=caps.checkpoint_level.value,
            )

            result = await self._a2a_adapter.delegate_task(alias, task_input, skill_id)

            # Capability-aware result rewriting (boundary §10)
            result = self._apply_capability_downgrade(result, caps, alias)

            if self._hook_dispatcher is not None:
                await self._hook_dispatcher.fire_advisory(
                    HookPoint.POST_DELEGATION,
                    payload=delegation_post_payload(
                        result.spawn_id, result.success, 0,
                    ),
                )
            return result
        except TimeoutError:
            if self._hook_dispatcher is not None:
                await self._hook_dispatcher.fire_advisory(
                    HookPoint.DELEGATION_ERROR,
                    payload=delegation_error_payload("", "timeout"),
                )
            return SubAgentResult(
                spawn_id="", success=False,
                error=f"{DelegationErrorCode.TIMEOUT}: A2A delegation timed out for {agent_url}",
            )
        except Exception as e:
            if self._hook_dispatcher is not None:
                await self._hook_dispatcher.fire_advisory(
                    HookPoint.DELEGATION_ERROR,
                    payload=delegation_error_payload("", str(e)),
                )
            return SubAgentResult(
                spawn_id="", success=False,
                error=f"{DelegationErrorCode.DELEGATION_FAILED}: {e}",
            )

    async def resume_a2a(
        self,
        remote_task_id: str,
        resume_payload: dict,
    ) -> SubAgentResult:
        """Resume a waiting remote A2A agent with additional input."""
        if self._a2a_adapter is None:
            return SubAgentResult(
                spawn_id=remote_task_id, success=False,
                error=f"{DelegationErrorCode.REMOTE_UNAVAILABLE}: A2A adapter not configured",
            )

        logger.info(
            "delegation.a2a.resume",
            remote_task_id=remote_task_id,
        )

        try:
            result = await self._a2a_adapter.resume_task(
                remote_task_id, resume_payload
            )
            return result
        except Exception as e:
            logger.error(
                "delegation.a2a.resume_failed",
                remote_task_id=remote_task_id, error=str(e),
            )
            return SubAgentResult(
                spawn_id=remote_task_id, success=False,
                error=f"{DelegationErrorCode.DELEGATION_FAILED}: Resume failed: {e}",
            )

    async def delegate_to_a2a_streaming(
        self,
        agent_url: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream delegation results from a remote A2A agent."""
        if self._a2a_adapter is None:
            yield {"type": "error", "data": "A2A adapter not configured"}
            return

        alias = self._a2a_adapter.resolve_alias(agent_url)
        if alias is None:
            try:
                await self._a2a_adapter.discover_agent(agent_url)
                alias = self._a2a_adapter.resolve_alias(agent_url)
            except Exception:
                pass
        if alias is None:
            yield {"type": "error", "data": f"Agent at {agent_url} not discoverable"}
            return

        async for event in self._a2a_adapter.delegate_task_streaming(
            alias, task_input, skill_id
        ):
            yield event

    # ------------------------------------------------------------------
    # Result summarization
    # ------------------------------------------------------------------

    @staticmethod
    def summarize_result(result: SubAgentResult) -> DelegationSummary:
        """Convert a SubAgentResult to a DelegationSummary for LLM consumption."""
        summary_text = result.final_answer or result.error or ""

        if result.success:
            summary_text += (
                "\n\n[Sub-agent task completed successfully. "
                "Summarize this result for the user. Do NOT call spawn_agent again.]"
            )
        elif result.suspend_info is not None:
            summary_text += (
                f"\n\n[Sub-agent is suspended: {result.suspend_info.reason.value}. "
                f"Message: {result.suspend_info.message}. "
                "Use resume_subagent to continue.]"
            )

        error_code: str | None = None
        if not result.success and result.suspend_info is None:
            error_code = result.error_code or DelegationExecutor._classify_error_code(result.error)

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
    def _apply_capability_downgrade(
        result: SubAgentResult,
        caps: Any,
        alias: str,
    ) -> SubAgentResult:
        """Rewrite result when remote status exceeds declared capabilities (§10).

        If remote returned a status it doesn't declare support for, downgrade
        the result to FAILED with an explicit capability mismatch error.
        """
        # WAITING_USER without typed question support → FAILED
        if (
            result.final_status == SubAgentStatus.WAITING_USER
            and not caps.supports_typed_questions
        ):
            logger.warning(
                "delegation.a2a.capability_downgrade",
                alias=alias,
                original_status="WAITING_USER",
                reason="Remote does not declare supports_typed_questions",
            )
            return result.model_copy(update={
                "success": False,
                "final_status": SubAgentStatus.FAILED,
                "error": (
                    f"A2A agent '{alias}' returned WAITING_USER but does not "
                    "declare supports_typed_questions capability. "
                    "Cannot forward input request."
                ),
            })

        # SUSPENDED without suspend_resume support → DEGRADED
        if (
            result.final_status == SubAgentStatus.SUSPENDED
            and not caps.supports_suspend_resume
        ):
            logger.warning(
                "delegation.a2a.capability_downgrade",
                alias=alias,
                original_status="SUSPENDED",
                reason="Remote does not declare supports_suspend_resume",
            )
            return result.model_copy(update={
                "success": False,
                "final_status": SubAgentStatus.FAILED,
                "error": (
                    f"A2A agent '{alias}' returned SUSPENDED but does not "
                    "declare supports_suspend_resume capability. "
                    "Cannot resume."
                ),
            })

        return result

    @staticmethod
    def _classify_error_code(error: str | None) -> str:
        """Map error messages to unified DelegationErrorCode."""
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


def _parent_id(parent_agent: Any) -> str:
    """Extract parent agent ID for logging."""
    if parent_agent is None:
        return "none"
    return getattr(parent_agent, "agent_id", "unknown")

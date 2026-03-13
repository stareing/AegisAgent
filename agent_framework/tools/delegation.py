from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import DelegationSummary, SubAgentResult, SubAgentSpec

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
    ) -> None:
        self._sub_agent_runtime = sub_agent_runtime
        self._a2a_adapter: Any = None

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

        # BaseAgent hook check
        if parent_agent is not None and hasattr(parent_agent, "on_spawn_requested"):
            allowed = await parent_agent.on_spawn_requested(spec)
            if not allowed:
                logger.warning(
                    "delegation.subagent.hook_denied",
                    parent_agent_id=parent_id,
                    reason="on_spawn_requested returned False",
                )
                return SubAgentResult(
                    spawn_id=spec.spawn_id,
                    success=False,
                    error="PERMISSION_DENIED: spawn rejected by parent agent hook",
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
        return await self._sub_agent_runtime.spawn(spec, parent_agent)

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
                spawn_id="",
                success=False,
                error="A2A adapter not configured",
            )

        # Find alias by URL
        alias = None
        for a, info in self._a2a_adapter._known_agents.items():
            if info.get("url") == agent_url:
                alias = a
                break

        if alias is None:
            # Try to discover on the fly
            try:
                await self._a2a_adapter.discover_agent(agent_url)
                for a, info in self._a2a_adapter._known_agents.items():
                    if info.get("url") == agent_url:
                        alias = a
                        break
            except Exception:
                pass

        if alias is None:
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"A2A agent at {agent_url} not discoverable",
            )

        return await self._a2a_adapter.delegate_task(alias, task_input, skill_id)

    @staticmethod
    def summarize_result(result: SubAgentResult) -> DelegationSummary:
        """Convert a SubAgentResult to a DelegationSummary for LLM consumption."""
        summary_text = result.final_answer or result.error or ""
        # Add termination hint so the model knows to stop calling spawn_agent
        if result.success:
            summary_text += (
                "\n\n[Sub-agent task completed successfully. "
                "Summarize this result for the user. Do NOT call spawn_agent again.]"
            )
        return DelegationSummary(
            status="success" if result.success else "failed",
            summary=summary_text,
            artifacts_digest=[a.name for a in result.artifacts],
            error_code=None if result.success else "DELEGATION_FAILED",
        )

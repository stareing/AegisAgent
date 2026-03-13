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
        # Check: SubAgentRuntime must be configured
        if self._sub_agent_runtime is None:
            return SubAgentResult(
                spawn_id=spec.spawn_id,
                success=False,
                error="SubAgentRuntime not configured",
            )

        # Doc 16.2 step 1: allow_spawn check
        if parent_agent is not None:
            config = getattr(parent_agent, "agent_config", None)
            if config is not None:
                # Doc 20.2: sub-agents have allow_spawn_children=False,
                # so they will be blocked here
                if not getattr(config, "allow_spawn_children", True):
                    logger.warning(
                        "subagent.spawn_denied",
                        agent_id=getattr(parent_agent, "agent_id", "unknown"),
                        reason="allow_spawn_children=False",
                    )
                    return SubAgentResult(
                        spawn_id=spec.spawn_id,
                        success=False,
                        error="PERMISSION_DENIED: This agent is not allowed to spawn children",
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
        return DelegationSummary(
            status="success" if result.success else "failed",
            summary=result.final_answer or result.error or "",
            artifacts_digest=[a.name for a in result.artifacts],
            error_code=None if result.success else "DELEGATION_FAILED",
        )

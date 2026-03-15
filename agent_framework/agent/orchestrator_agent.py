"""OrchestratorAgent — main agent with multi-agent coordination capability.

Extends BaseAgent with orchestration-aware system prompt, spawn permission,
and hard exit guards that complement prompt-based soft constraints.

Two layers of control:
- Soft (prompt): teaches the LLM when/how to delegate and synthesize
- Hard (code): should_stop enforces iteration/spawn budget limits
"""

from __future__ import annotations

from agent_framework.agent.base_agent import BaseAgent
from agent_framework.agent.prompt_templates import ORCHESTRATOR_SYSTEM_PROMPT
from agent_framework.models.agent import (
    AgentConfig,
    AgentState,
    IterationResult,
    SpawnDecision,
    StopDecision,
    StopReason,
    StopSignal,
)


# After all spawns complete, allow at most N iterations for synthesis.
# <= 0 means unlimited.
_MAX_POST_SPAWN_ITERATIONS = 0


class OrchestratorAgent(BaseAgent):
    """Main agent with orchestrator capability.

    Differences from DefaultAgent:
    - Uses ORCHESTRATOR_SYSTEM_PROMPT (delegation-aware instructions)
    - allow_spawn_children=True by default
    - Relaxed spawn policy (approves by default, logs for audit)
    - Hard exit guard: forces stop if LLM keeps iterating after all spawns complete
    """

    def __init__(
        self,
        agent_id: str = "orchestrator",
        system_prompt: str = "",
        model_name: str = "gpt-3.5-turbo",
        max_iterations: int = 0,
        temperature: float = 1.0,
        max_output_tokens: int = 4096,
        allow_spawn_children: bool = True,
        max_concurrent_tool_calls: int = 5,
        allow_parallel_tool_calls: bool = True,
    ) -> None:
        config = AgentConfig(
            agent_id=agent_id,
            system_prompt=system_prompt or ORCHESTRATOR_SYSTEM_PROMPT,
            model_name=model_name,
            max_iterations=max_iterations,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            allow_spawn_children=allow_spawn_children,
            max_concurrent_tool_calls=max_concurrent_tool_calls,
            allow_parallel_tool_calls=allow_parallel_tool_calls,
        )
        super().__init__(config)

    async def on_spawn_requested(self, spawn_spec: object) -> SpawnDecision:
        """Approve spawn requests with audit logging.

        OrchestratorAgent approves all spawns by default since its
        purpose is to coordinate sub-agents. The SubAgentScheduler
        handles quota enforcement separately.
        """
        return SpawnDecision(
            allowed=True,
            reason="Orchestrator approves delegation",
            source="OrchestratorAgent",
        )

    def should_stop(
        self, iteration_result: IterationResult, agent_state: AgentState
    ) -> StopDecision:
        """Orchestrator stop logic — adds hard exit guard on top of base checks.

        Hard constraints (code-enforced, not prompt-dependent):
        1. Base class checks (max_iterations, stop_signal) — always apply
        2. Post-spawn synthesis budget: if spawns happened but LLM keeps
           iterating without spawning more, force stop after N iterations
           to prevent runaway loops
        """
        # Base class checks first
        parent_decision = super().should_stop(iteration_result, agent_state)
        if parent_decision.should_stop:
            return parent_decision

        # Hard guard: if enabled, enforce synthesis budget after the last spawn
        if _MAX_POST_SPAWN_ITERATIONS > 0 and agent_state.spawn_count > 0:
            # Find the last iteration that contained a spawn_agent result
            last_spawn_iter = -1
            for i, it in enumerate(agent_state.iteration_history):
                for tr in it.tool_results:
                    if tr.tool_name == "spawn_agent":
                        last_spawn_iter = i
            # Count iterations since last spawn
            current_iter = len(agent_state.iteration_history) - 1
            iters_since_last_spawn = current_iter - last_spawn_iter

            if iters_since_last_spawn >= _MAX_POST_SPAWN_ITERATIONS:
                return StopDecision(
                    should_stop=True,
                    reason=(
                        f"Orchestrator exceeded synthesis budget: "
                        f"{iters_since_last_spawn} iterations after last spawn "
                        f"(limit: {_MAX_POST_SPAWN_ITERATIONS}). "
                        f"Total spawns: {agent_state.spawn_count}."
                    ),
                    source="OrchestratorAgent.hard_guard",
                    stop_signal=StopSignal(
                        reason=StopReason.MAX_ITERATIONS,
                        message="Post-spawn synthesis budget exceeded",
                    ),
                )

        return StopDecision(should_stop=False, source="OrchestratorAgent")

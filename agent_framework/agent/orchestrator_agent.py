"""OrchestratorAgent — main agent with multi-agent coordination capability.

Extends DefaultAgent with orchestration-aware system prompt and spawn
permission. Used as the default main agent when the framework is
configured to allow sub-agent spawning.

The orchestration intelligence lives in the system prompt, not in code.
The agent uses the existing spawn_agent tool to delegate tasks, and the
existing SubAgentScheduler/Runtime handles execution.
"""

from __future__ import annotations

from agent_framework.agent.base_agent import BaseAgent
from agent_framework.agent.prompt_templates import ORCHESTRATOR_SYSTEM_PROMPT
from agent_framework.models.agent import AgentConfig, SpawnDecision


class OrchestratorAgent(BaseAgent):
    """Main agent with orchestrator capability.

    Differences from DefaultAgent:
    - Uses ORCHESTRATOR_SYSTEM_PROMPT (delegation-aware instructions)
    - allow_spawn_children=True by default
    - Relaxed spawn policy (approves by default, logs for audit)
    """

    def __init__(
        self,
        agent_id: str = "orchestrator",
        system_prompt: str = "",
        model_name: str = "gpt-3.5-turbo",
        max_iterations: int = 30,
        temperature: float = 0.7,
    ) -> None:
        config = AgentConfig(
            agent_id=agent_id,
            system_prompt=system_prompt or ORCHESTRATOR_SYSTEM_PROMPT,
            model_name=model_name,
            max_iterations=max_iterations,
            temperature=temperature,
            allow_spawn_children=True,
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

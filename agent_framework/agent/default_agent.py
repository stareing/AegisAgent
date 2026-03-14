from __future__ import annotations

from agent_framework.agent.base_agent import BaseAgent
from agent_framework.agent.prompt_templates import DEFAULT_SYSTEM_PROMPT
from agent_framework.models.agent import AgentConfig


class DefaultAgent(BaseAgent):
    """Default agent implementation with no custom hooks.

    Uses all default behaviors from BaseAgent.
    Good starting point for simple use cases.
    """

    def __init__(
        self,
        agent_id: str = "default",
        system_prompt: str = "",
        model_name: str = "gpt-3.5-turbo",
        max_iterations: int = 20,
        temperature: float = 0.7,
        allow_spawn_children: bool = False,
        max_concurrent_tool_calls: int = 5,
        allow_parallel_tool_calls: bool = True,
    ) -> None:
        config = AgentConfig(
            agent_id=agent_id,
            system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
            model_name=model_name,
            max_iterations=max_iterations,
            temperature=temperature,
            allow_spawn_children=allow_spawn_children,
            max_concurrent_tool_calls=max_concurrent_tool_calls,
            allow_parallel_tool_calls=allow_parallel_tool_calls,
        )
        super().__init__(config)

from __future__ import annotations

import re

from agent_framework.agent.base_agent import BaseAgent
from agent_framework.agent.prompt_templates import REACT_SYSTEM_PROMPT
from agent_framework.models.agent import (
    AgentConfig,
    AgentState,
    ErrorStrategy,
    IterationResult,
    StopReason,
    StopSignal,
)

_FINAL_ANSWER_PATTERN = re.compile(
    r"Final\s*Answer\s*[:：]\s*(.*)", re.IGNORECASE | re.DOTALL
)


class ReActAgent(BaseAgent):
    """ReAct pattern agent: Thought → Action → Observation → Final Answer.

    Extends BaseAgent with:
    - ReAct-specific system prompt (prepended to user system prompt)
    - Custom should_stop() detecting "Final Answer:" pattern in model output
    - Configurable max_react_steps for additional iteration control
    """

    def __init__(
        self,
        agent_id: str = "react",
        system_prompt: str = "",
        model_name: str = "gpt-4",
        max_iterations: int = 25,
        temperature: float = 0.2,
        max_react_steps: int | None = None,
        allow_spawn_children: bool = False,
    ) -> None:
        full_prompt = REACT_SYSTEM_PROMPT
        if system_prompt:
            full_prompt += f"\n## Additional Instructions\n{system_prompt}\n"

        config = AgentConfig(
            agent_id=agent_id,
            system_prompt=full_prompt,
            model_name=model_name,
            max_iterations=max_iterations,
            temperature=temperature,
            allow_spawn_children=allow_spawn_children,
        )
        super().__init__(config)
        self._max_react_steps = max_react_steps

    def should_stop(
        self, iteration_result: IterationResult, agent_state: AgentState
    ) -> bool:
        # Parent stop conditions (stop_signal, max_iterations)
        if super().should_stop(iteration_result, agent_state):
            return True

        # ReAct-specific: detect "Final Answer:" in model output
        if iteration_result.model_response and iteration_result.model_response.content:
            content = iteration_result.model_response.content
            match = _FINAL_ANSWER_PATTERN.search(content)
            if match:
                # Inject stop signal so coordinator can extract the answer
                iteration_result.stop_signal = StopSignal(
                    reason=StopReason.CUSTOM,
                    message="ReAct final answer detected",
                )
                return True

        # Optional react step limit
        if self._max_react_steps and agent_state.iteration_count >= self._max_react_steps:
            iteration_result.stop_signal = StopSignal(
                reason=StopReason.MAX_ITERATIONS,
                message=f"Reached max ReAct steps ({self._max_react_steps})",
            )
            return True

        return False

    def get_error_policy(
        self, error: Exception, agent_state: AgentState
    ) -> ErrorStrategy:
        """ReAct agents retry on transient errors to maintain reasoning chain."""
        if agent_state.iteration_count < self.agent_config.max_iterations - 1:
            return ErrorStrategy.RETRY
        return ErrorStrategy.ABORT

    @staticmethod
    def extract_final_answer(content: str) -> str | None:
        """Extract the final answer text from model output."""
        match = _FINAL_ANSWER_PATTERN.search(content)
        if match:
            return match.group(1).strip()
        return None

from __future__ import annotations

import re

from agent_framework.agent.base_agent import BaseAgent
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

_REACT_SYSTEM_PROMPT = """\
You are a ReAct (Reasoning + Acting) agent. You solve tasks by interleaving Thought, Action, and Observation steps.

## Protocol
1. **Thought**: Analyze the current situation and decide what to do next.
2. **Action**: Use one of the available tools to gather information or perform an action.
3. **Observation**: Review the tool result and reason about the next step.
4. Repeat until you have enough information to answer.
5. When ready, respond with: **Final Answer: <your answer>**

## Rules
- Always think step-by-step before acting.
- Use tools to verify information rather than guessing.
- If a tool call fails, reason about why and try an alternative approach.
- Do NOT fabricate tool results or information.
- When you are confident in your answer, output "Final Answer:" followed by your complete response.
"""


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
        full_prompt = _REACT_SYSTEM_PROMPT
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

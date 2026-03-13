"""RunPolicyResolver — builds EffectiveRunConfig from AgentConfig + Skill.

Single responsibility: policy composition. RunCoordinator delegates all
config-building logic here instead of inlining it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.models.agent import EffectiveRunConfig

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.models.agent import Skill


class RunPolicyResolver:
    """Resolves run-time configuration from agent config + skill overrides.

    Rules (v2.4 §8):
    - Skill can only override whitelisted fields (model_name, temperature).
    - Safety fields (max_iterations, max_output_tokens) are never overridden.
    """

    @staticmethod
    def build_effective_config(
        agent: BaseAgent, active_skill: Skill | None
    ) -> EffectiveRunConfig:
        cfg = agent.agent_config
        model_name = cfg.model_name
        temperature = cfg.temperature

        # Skill override — whitelist only (v2.4 §8)
        if active_skill is not None:
            if active_skill.model_override:
                model_name = active_skill.model_override
            if active_skill.temperature_override is not None:
                temperature = active_skill.temperature_override

        return EffectiveRunConfig(
            model_name=model_name,
            temperature=temperature,
            max_output_tokens=cfg.max_output_tokens,
            max_iterations=cfg.max_iterations,
        )

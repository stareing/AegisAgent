"""RunPolicyResolver — builds run-scoped policy bundle from AgentConfig + Skill.

Single responsibility: policy composition. RunCoordinator delegates all
config-building logic here instead of inlining it.

v2.6.1 §30: This is the SOLE entry point for EffectiveRunConfig creation.
RunCoordinator MUST NOT construct EffectiveRunConfig directly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from agent_framework.models.agent import (
    CapabilityPolicy,
    ContextPolicy,
    EffectiveRunConfig,
    MemoryPolicy,
)

if TYPE_CHECKING:
    from agent_framework.agent.base_agent import BaseAgent
    from agent_framework.models.agent import AgentState, Skill


class ResolvedRunPolicyBundle(BaseModel):
    """Complete run-scoped policy bundle (v2.6.1 §30).

    Produced exclusively by RunPolicyResolver. RunCoordinator consumes
    this bundle and passes individual policies to their authorized
    consumers (ContextEngineer, MemoryManager, authorization chain).

    No other module may construct or patch this bundle.
    """

    model_config = {"frozen": True}

    effective_run_config: EffectiveRunConfig
    context_policy: ContextPolicy
    memory_policy: MemoryPolicy
    capability_policy: CapabilityPolicy


class RunPolicyResolver:
    """Resolves run-time configuration from agent config + skill overrides.

    Rules (v2.4 §8, v2.6.1 §30):
    - Skill can only override whitelisted fields (model_name, temperature).
    - Safety fields (max_iterations, max_output_tokens) are never overridden.
    - This class is the SOLE producer of EffectiveRunConfig and ResolvedRunPolicyBundle.

    Prohibited:
    - RunCoordinator constructing EffectiveRunConfig directly.
    - DelegationExecutor re-merging parent run config.
    - AgentLoop patching config based on local conditions.
    """

    @staticmethod
    def build_effective_config(
        agent: BaseAgent, active_skill: Skill | None
    ) -> EffectiveRunConfig:
        """Build EffectiveRunConfig. Retained for backward compatibility.

        Prefer resolve_run_policy_bundle() for new code.
        """
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

    @staticmethod
    def resolve_run_policy_bundle(
        agent: BaseAgent,
        active_skill: Skill | None,
        agent_state: AgentState,
    ) -> ResolvedRunPolicyBundle:
        """Build the complete run policy bundle (v2.6.1 §30).

        This is the single entry point for all run-scoped configuration.
        RunCoordinator calls this once at run start and consumes the result.
        """
        effective_config = RunPolicyResolver.build_effective_config(agent, active_skill)
        context_policy = agent.get_context_policy(agent_state)
        memory_policy = agent.get_memory_policy(agent_state)
        capability_policy = agent.get_capability_policy()

        return ResolvedRunPolicyBundle(
            effective_run_config=effective_config,
            context_policy=context_policy,
            memory_policy=memory_policy,
            capability_policy=capability_policy,
        )

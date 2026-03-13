from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.models.agent import Skill

if TYPE_CHECKING:
    from agent_framework.protocols.core import ContextEngineerProtocol


class SkillRouter:
    """Routes user input to skills and manages skill activation."""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}
        self._active_skill: Skill | None = None

    def register_skill(self, skill: Skill) -> None:
        self._skills[skill.skill_id] = skill

    def detect_skill(self, user_input: str) -> Skill | None:
        """Detect if user input triggers a skill via keywords."""
        input_lower = user_input.lower()
        for skill in self._skills.values():
            for keyword in skill.trigger_keywords:
                if keyword.lower() in input_lower:
                    return skill
        return None

    def activate_skill(
        self, skill: Skill, context_engineer: ContextEngineerProtocol
    ) -> None:
        """Activate a skill, injecting its prompt addon."""
        self._active_skill = skill
        context_engineer.set_skill_context(skill.system_prompt_addon)

    def deactivate_current_skill(self) -> None:
        self._active_skill = None

    def get_active_skill(self) -> Skill | None:
        return self._active_skill

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

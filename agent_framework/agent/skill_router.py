from __future__ import annotations

from typing import TYPE_CHECKING

from agent_framework.models.agent import Skill

if TYPE_CHECKING:
    from agent_framework.protocols.core import ContextEngineerProtocol


class SkillRouter:
    """Skill registry and keyword-based detection.

    Ownership boundary:
    - SkillRouter owns the skill CATALOG (registration + detection).
    - SkillRouter does NOT own active skill state — that belongs to
      the run-scoped AgentState (managed by RunCoordinator).
    - This separation ensures multiple concurrent runs cannot interfere
      with each other's active skill.

    Prohibited:
    - Do NOT store _active_skill or any per-run mutable state here.
    - Do NOT store references to ContextEngineer or other run-scoped objects.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

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

    def get_skill(self, skill_id: str) -> Skill | None:
        """Look up a skill by ID."""
        return self._skills.get(skill_id)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

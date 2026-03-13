from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger
from agent_framework.models.agent import Skill

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class SkillRouter:
    """Skill registry, keyword detection, and file-based skill discovery.

    Ownership boundary:
    - SkillRouter owns the skill CATALOG (registration + detection + descriptions).
    - SkillRouter does NOT own active skill state — that belongs to
      the run-scoped AgentState (managed by RunCoordinator).
    - This separation ensures multiple concurrent runs cannot interfere
      with each other's active skill.

    Supports two skill sources:
    - Config/programmatic: trigger_keywords + system_prompt_addon
    - File-based (SKILL.md): description-based, LLM invokes via invoke_skill tool

    Prohibited:
    - Do NOT store _active_skill or any per-run mutable state here.
    - Do NOT store references to ContextEngineer or other run-scoped objects.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register_skill(self, skill: Skill) -> None:
        self._skills[skill.skill_id] = skill

    def detect_skill(self, user_input: str) -> Skill | None:
        """Detect if user input triggers a skill via keywords.

        Only applies to skills with trigger_keywords (config-based).
        File-based skills are triggered by the LLM via invoke_skill tool.
        """
        input_lower = user_input.lower()
        for skill in self._skills.values():
            if not skill.trigger_keywords:
                continue
            for keyword in skill.trigger_keywords:
                if keyword.lower() in input_lower:
                    return skill
        return None

    def get_skill(self, skill_id: str) -> Skill | None:
        """Look up a skill by ID."""
        return self._skills.get(skill_id)

    def list_skills(self) -> list[Skill]:
        return list(self._skills.values())

    # ------------------------------------------------------------------
    # File-based skill support
    # ------------------------------------------------------------------

    def load_file_skills(self, directories: list[Path]) -> int:
        """Discover and register skills from SKILL.md files.

        Returns number of skills loaded.
        """
        from agent_framework.skills.loader import discover_skills

        discovered = discover_skills(directories)
        count = 0
        for parsed in discovered:
            skill_id = parsed["skill_id"]
            if skill_id in self._skills:
                logger.info("skill.file_skip_duplicate", skill_id=skill_id)
                continue

            fm = parsed["frontmatter"]
            skill = Skill(
                skill_id=skill_id,
                name=fm.get("name", skill_id),
                description=fm.get("description", ""),
                source_path=str(parsed["path"]),
                allowed_tools=fm.get("allowed-tools") if isinstance(fm.get("allowed-tools"), list) else None,
                disable_model_invocation=bool(fm.get("disable-model-invocation", False)),
                user_invocable=bool(fm.get("user-invocable", True)),
                argument_hint=fm.get("argument-hint", ""),
                model_override=fm.get("model"),
                temperature_override=float(fm["temperature"]) if "temperature" in fm else None,
            )
            self._skills[skill_id] = skill
            count += 1
            logger.info(
                "skill.file_registered",
                skill_id=skill_id,
                source=str(parsed["path"]),
                has_description=bool(skill.description),
            )

        return count

    def get_skill_descriptions(self) -> list[dict[str, str]]:
        """Return lightweight descriptions for context injection.

        Only includes skills that allow model invocation.
        The LLM uses these to decide when to invoke a skill.
        """
        result = []
        for skill in self._skills.values():
            if skill.disable_model_invocation:
                continue
            if not skill.description:
                continue
            entry = {
                "skill_id": skill.skill_id,
                "name": skill.name or skill.skill_id,
                "description": skill.description,
            }
            if skill.argument_hint:
                entry["argument_hint"] = skill.argument_hint
            result.append(entry)
        return result

    def get_file_skills(self) -> list[Skill]:
        """Return only file-based skills (those with source_path)."""
        return [s for s in self._skills.values() if s.source_path]

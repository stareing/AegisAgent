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

    def remove_skill(self, skill_id: str) -> bool:
        """Remove a skill by ID. Returns True if removed, False if not found."""
        return self._skills.pop(skill_id, None) is not None

    def register_skills(self, skills: list[Skill]) -> None:
        """Batch register multiple skills."""
        for skill in skills:
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

    @staticmethod
    def _parsed_to_skill(
        parsed: dict,
        *,
        source_label: str | None = None,
        priority: int = 0,
    ) -> Skill:
        """Map a parsed SKILL.md dict to a Skill model instance."""
        fm = parsed["frontmatter"]
        skill_id = parsed["skill_id"]

        return Skill(
            skill_id=skill_id,
            name=fm.get("name", skill_id),
            description=fm.get("description", ""),
            source_path=str(parsed["path"]),
            allowed_tools=(
                fm.get("allowed-tools")
                if isinstance(fm.get("allowed-tools"), list)
                else None
            ),
            disable_model_invocation=bool(
                fm.get("disable-model-invocation", False)
            ),
            user_invocable=bool(fm.get("user-invocable", True)),
            argument_hint=fm.get("argument-hint", ""),
            model_override=fm.get("model"),
            temperature_override=(
                float(fm["temperature"]) if "temperature" in fm else None
            ),
            # v4.0 fields from enriched frontmatter
            execution_mode=parsed.get("execution_mode", "inline"),
            effort_level=parsed.get("effort_level"),
            hooks=parsed.get("hooks") if isinstance(parsed.get("hooks"), dict) else {},
            paths=parsed.get("paths") if isinstance(parsed.get("paths"), list) else [],
            arguments=(
                parsed.get("arguments")
                if isinstance(parsed.get("arguments"), list)
                else []
            ),
            skill_source=source_label or parsed.get("source_label", "project"),
            priority=parsed.get("priority", priority),
        )

    def load_file_skills(self, directories: list[Path]) -> int:
        """Discover and register skills from SKILL.md files.

        Returns number of skills loaded. Backward-compatible entry point.
        """
        from agent_framework.skills.loader import discover_skills

        discovered = discover_skills(directories)
        count = 0
        for parsed in discovered:
            skill_id = parsed["skill_id"]
            if skill_id in self._skills:
                logger.info("skill.file_skip_duplicate", skill_id=skill_id)
                continue

            skill = self._parsed_to_skill(parsed)
            self._skills[skill_id] = skill
            count += 1
            logger.info(
                "skill.file_registered",
                skill_id=skill_id,
                source=str(parsed["path"]),
                has_description=bool(skill.description),
            )

        return count

    def load_all_skills(
        self,
        builtin_dirs: list[Path] | None = None,
        user_dirs: list[Path] | None = None,
        project_dirs: list[Path] | None = None,
        policy_dirs: list[Path] | None = None,
        extra_dirs: list[Path] | None = None,
        mcp_skills: list[Skill] | None = None,
    ) -> int:
        """Load skills from multiple prioritized sources.

        Priority ladder (higher overrides lower for the same skill_id):
            builtin=0  →  user=1  →  project=2  →  policy=3  →  extra=4  →  mcp=5

        Args:
            builtin_dirs: Framework built-in skill directories.
            user_dirs: User-level skill directories (~/.agent/skills).
            project_dirs: Project-level directories (skills/, .skills/).
            policy_dirs: Config-specified extra directories.
            extra_dirs: Additional directories (catch-all).
            mcp_skills: Pre-built Skill objects from MCP servers.

        Returns:
            Total number of skills registered from this call.
        """
        from agent_framework.skills.loader import discover_skills_with_priority

        _SOURCE_TIERS: list[tuple[list[Path] | None, str, int]] = [
            (builtin_dirs, "builtin", 0),
            (user_dirs, "user", 1),
            (project_dirs, "project", 2),
            (policy_dirs, "policy", 3),
            (extra_dirs, "extra", 4),
        ]

        sources: list[tuple[Path, str, int]] = []
        for dirs, label, pri in _SOURCE_TIERS:
            if dirs:
                for d in dirs:
                    sources.append((d, label, pri))

        discovered = discover_skills_with_priority(sources)

        count = 0
        for parsed in discovered:
            skill_id = parsed["skill_id"]
            source_label = parsed.get("source_label", "project")
            priority = parsed.get("priority", 0)

            # Allow priority override of previously registered (e.g. config) skills
            existing = self._skills.get(skill_id)
            if existing is not None and existing.priority > priority:
                logger.info(
                    "skill.skip_lower_priority",
                    skill_id=skill_id,
                    existing_priority=existing.priority,
                    new_priority=priority,
                )
                continue

            skill = self._parsed_to_skill(
                parsed, source_label=source_label, priority=priority
            )
            self._skills[skill_id] = skill
            count += 1
            logger.info(
                "skill.file_registered",
                skill_id=skill_id,
                source=str(parsed["path"]),
                source_label=source_label,
                priority=priority,
                has_description=bool(skill.description),
            )

        # MCP skills override all file-based skills (priority=5)
        if mcp_skills:
            for mcp_skill in mcp_skills:
                self._skills[mcp_skill.skill_id] = mcp_skill
                count += 1
                logger.info(
                    "skill.mcp_registered",
                    skill_id=mcp_skill.skill_id,
                    source="mcp",
                    priority=5,
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

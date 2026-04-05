"""Built-in skill invocation tool.

Allows the LLM to invoke registered skills by ID with arguments.
The skill body is lazy-loaded from SKILL.md, preprocessed, and
returned as tool output for the LLM to follow.
"""

from __future__ import annotations

from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.tools.decorator import tool

logger = get_logger(__name__)

# Module-level reference set during framework setup.
_skill_router: Any = None
_context_engineer: Any = None


def set_skill_runtime(skill_router: Any, context_engineer: Any) -> None:
    """Wire runtime references. Called once during framework setup."""
    global _skill_router, _context_engineer
    _skill_router = skill_router
    _context_engineer = context_engineer


@tool(
    name="invoke_skill",
    description=(
        "Invoke a registered skill by its skill_id. "
        "Use this when the user's request matches a skill description. "
        "The skill provides detailed instructions for handling the task. "
        "Pass any relevant arguments from the user's message."
    ),
    category="skill",
    require_confirm=False,
    is_read_only=True,
    search_hint="invoke activate skill",
)
def invoke_skill(skill_id: str, arguments: str = "") -> dict:
    """Invoke a skill and return structured result for the LLM.

    Args:
        skill_id: The ID of the skill to invoke.
        arguments: Free-form arguments from the user's message.

    Returns:
        Dict with ``success``, ``content``, metadata on success;
        ``success=False`` and ``error`` on failure.
    """
    if _skill_router is None:
        return {"success": False, "error": "Skill system not initialized"}

    skill = _skill_router.get_skill(skill_id)
    if skill is None:
        available = [s.skill_id for s in _skill_router.list_skills()]
        return {
            "success": False,
            "error": (
                f"Skill '{skill_id}' not found. "
                f"Available: {', '.join(available)}"
            ),
        }

    # File-based skill: lazy load body from SKILL.md
    if skill.source_path:
        from pathlib import Path

        from agent_framework.skills.loader import load_skill_body
        from agent_framework.skills.preprocessor import preprocess_skill

        try:
            body = load_skill_body(skill.source_path)
        except FileNotFoundError:
            return {
                "success": False,
                "error": f"Skill file not found: {skill.source_path}",
            }

        # skill_dir = directory containing SKILL.md (for ${SKILL_DIR} and cwd)
        skill_dir = str(Path(skill.source_path).parent)

        body = preprocess_skill(
            body,
            raw_args=arguments,
            skill_dir=skill_dir,
            enable_shell=True,
        )

        logger.info(
            "skill.invoked",
            skill_id=skill_id,
            source="file",
            skill_dir=skill_dir,
            args_preview=arguments[:100],
            body_length=len(body),
        )
        return {
            "success": True,
            "content": body,
            "skill_id": skill_id,
            "skill_name": skill.name,
            "skill_dir": skill_dir,
            "source": skill.skill_source,
            "execution_mode": skill.execution_mode,
        }

    # Config-based skill: return system_prompt_addon as the instruction
    if skill.system_prompt_addon:
        logger.info(
            "skill.invoked",
            skill_id=skill_id,
            source="config",
            args_preview=arguments[:100],
        )
        addon = skill.system_prompt_addon
        if arguments:
            addon = f"{addon}\n\nARGUMENTS: {arguments}"
        return {
            "success": True,
            "content": addon,
            "skill_id": skill_id,
            "skill_name": skill.name,
            "skill_dir": None,
            "source": skill.skill_source,
            "execution_mode": skill.execution_mode,
        }

    return {
        "success": False,
        "error": f"Skill '{skill_id}' has no content to invoke.",
    }

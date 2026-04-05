"""Tests for priority-based skill loading, realpath dedup, and structured returns."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from agent_framework.agent.skill_router import SkillRouter
from agent_framework.models.agent import Skill
from agent_framework.skills.loader import (
    discover_skills,
    discover_skills_with_priority,
    parse_skill_md,
)


# =====================================================================
# Helper: create a SKILL.md file with optional frontmatter
# =====================================================================

def _write_skill(
    directory: Path,
    name: str,
    *,
    description: str = "test skill",
    extra_fm: str = "",
    body: str = "Do the thing.",
    flat: bool = False,
) -> Path:
    """Create a SKILL.md (directory layout) or <name>.md (flat layout)."""
    if flat:
        skill_file = directory / f"{name}.md"
    else:
        skill_dir = directory / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file = skill_dir / "SKILL.md"

    fm_block = f"name: {name}\ndescription: {description}\n"
    if extra_fm:
        fm_block += extra_fm
    content = f"---\n{fm_block}---\n\n{body}\n"
    skill_file.write_text(content)
    return skill_file


# =====================================================================
# 1. Realpath-based dedup (symlink duplicates)
# =====================================================================

class TestRealpathDedup:

    def test_symlink_dedup_loads_once(self, tmp_path: Path):
        """Two directories containing a symlink to the same SKILL.md yield one skill."""
        dir_a = tmp_path / "dir_a"
        dir_b = tmp_path / "dir_b"
        dir_a.mkdir()
        dir_b.mkdir()

        # Real skill in dir_a
        real_file = _write_skill(dir_a, "my-skill")

        # Symlink entire skill directory into dir_b
        link_target = dir_a / "my-skill"
        link_path = dir_b / "my-skill"
        try:
            link_path.symlink_to(link_target)
        except OSError:
            pytest.skip("Symlinks not supported on this filesystem")

        sources = [
            (dir_a, "source_a", 0),
            (dir_b, "source_b", 1),
        ]
        result = discover_skills_with_priority(sources)
        assert len(result) == 1
        assert result[0]["skill_id"] == "my-skill"


# =====================================================================
# 2. Priority override
# =====================================================================

class TestPriorityOverride:

    def test_higher_priority_wins(self, tmp_path: Path):
        """Project skill (priority=2) overrides user skill (priority=1)."""
        user_dir = tmp_path / "user"
        project_dir = tmp_path / "project"

        _write_skill(user_dir, "deploy", description="user deploy")
        _write_skill(project_dir, "deploy", description="project deploy")

        sources = [
            (user_dir, "user", 1),
            (project_dir, "project", 2),
        ]
        result = discover_skills_with_priority(sources)
        assert len(result) == 1
        assert result[0]["frontmatter"]["description"] == "project deploy"
        assert result[0]["source_label"] == "project"
        assert result[0]["priority"] == 2

    def test_same_priority_last_wins(self, tmp_path: Path):
        """Two sources at same priority — the later one wins."""
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"

        _write_skill(dir_a, "lint", description="from a")
        _write_skill(dir_b, "lint", description="from b")

        sources = [
            (dir_a, "a", 0),
            (dir_b, "b", 0),
        ]
        result = discover_skills_with_priority(sources)
        assert len(result) == 1
        assert result[0]["frontmatter"]["description"] == "from b"

    def test_results_sorted_by_skill_id(self, tmp_path: Path):
        """Output is sorted alphabetically by skill_id."""
        d = tmp_path / "skills"
        _write_skill(d, "zebra")
        _write_skill(d, "alpha")
        _write_skill(d, "middle")

        sources = [(d, "project", 0)]
        result = discover_skills_with_priority(sources)
        ids = [r["skill_id"] for r in result]
        assert ids == ["alpha", "middle", "zebra"]


# =====================================================================
# 3. New v4.0 frontmatter fields
# =====================================================================

class TestV4FrontmatterFields:

    def test_execution_mode_parsed(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "my-skill", extra_fm="execution-mode: fork\n")
        result = discover_skills_with_priority([(d, "project", 0)])
        assert result[0]["execution_mode"] == "fork"

    def test_effort_level_parsed(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "my-skill", extra_fm="effort: extensive\n")
        result = discover_skills_with_priority([(d, "project", 0)])
        assert result[0]["effort_level"] == "extensive"

    def test_version_parsed(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "my-skill", extra_fm="version: 2.1.0\n")
        result = discover_skills_with_priority([(d, "project", 0)])
        assert result[0]["version"] == "2.1.0"

    def test_shell_parsed(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "my-skill", extra_fm="shell: bash\n")
        result = discover_skills_with_priority([(d, "project", 0)])
        assert result[0]["shell"] == "bash"

    def test_context_parsed(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "my-skill", extra_fm="context: fork\n")
        result = discover_skills_with_priority([(d, "project", 0)])
        assert result[0]["context"] == "fork"

    def test_agent_ref_parsed(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "my-skill", extra_fm="agent: code-reviewer\n")
        result = discover_skills_with_priority([(d, "project", 0)])
        assert result[0]["agent_ref"] == "code-reviewer"

    def test_paths_parsed_as_list(self, tmp_path: Path):
        d = tmp_path / "skills"
        extra = "paths:\n- src/\n- tests/\n"
        _write_skill(d, "my-skill", extra_fm=extra)
        result = discover_skills_with_priority([(d, "project", 0)])
        assert result[0]["paths"] == ["src/", "tests/"]

    def test_missing_v4_fields_not_in_result(self, tmp_path: Path):
        """When v4 frontmatter keys are absent, parsed dict omits them."""
        d = tmp_path / "skills"
        _write_skill(d, "basic")
        result = discover_skills_with_priority([(d, "project", 0)])
        assert "execution_mode" not in result[0]
        assert "effort_level" not in result[0]
        assert "version" not in result[0]


# =====================================================================
# 4. load_all_skills with multiple sources
# =====================================================================

class TestLoadAllSkills:

    def test_load_all_skills_basic(self, tmp_path: Path):
        user_dir = tmp_path / "user"
        project_dir = tmp_path / "project"

        _write_skill(user_dir, "user-only", description="user skill")
        _write_skill(project_dir, "project-only", description="project skill")

        router = SkillRouter()
        count = router.load_all_skills(
            user_dirs=[user_dir],
            project_dirs=[project_dir],
        )
        assert count == 2
        assert router.get_skill("user-only") is not None
        assert router.get_skill("project-only") is not None

    def test_load_all_skills_priority_override(self, tmp_path: Path):
        user_dir = tmp_path / "user"
        project_dir = tmp_path / "project"

        _write_skill(user_dir, "shared", description="user version")
        _write_skill(project_dir, "shared", description="project version")

        router = SkillRouter()
        count = router.load_all_skills(
            user_dirs=[user_dir],
            project_dirs=[project_dir],
        )
        # Only one skill registered (project wins)
        assert count == 1
        skill = router.get_skill("shared")
        assert skill is not None
        assert skill.description == "project version"
        assert skill.skill_source == "project"
        assert skill.priority == 2

    def test_load_all_skills_maps_v4_fields(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "fancy", extra_fm="execution-mode: fork\neffort: quick\n")

        router = SkillRouter()
        router.load_all_skills(project_dirs=[d])
        skill = router.get_skill("fancy")
        assert skill is not None
        assert skill.execution_mode == "fork"
        assert skill.effort_level == "quick"

    def test_mcp_skills_override_file(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "deploy", description="file version")

        mcp_skill = Skill(
            skill_id="deploy",
            name="deploy",
            description="mcp version",
            skill_source="mcp",
            priority=5,
        )

        router = SkillRouter()
        count = router.load_all_skills(
            project_dirs=[d],
            mcp_skills=[mcp_skill],
        )
        assert count == 2  # file + mcp both counted
        skill = router.get_skill("deploy")
        assert skill is not None
        assert skill.description == "mcp version"
        assert skill.skill_source == "mcp"

    def test_load_all_skills_no_dirs(self):
        """Calling with no directories does not crash."""
        router = SkillRouter()
        count = router.load_all_skills()
        assert count == 0


# =====================================================================
# 5. invoke_skill returns dict
# =====================================================================

class TestInvokeSkillStructuredReturn:

    def test_invoke_returns_dict_success(self, tmp_path: Path):
        from agent_framework.tools import builtin_skills

        d = tmp_path / "skills"
        skill_file = _write_skill(d, "greet", body="Hello $ARGUMENTS!")

        router = SkillRouter()
        router.load_all_skills(project_dirs=[d])

        # Wire runtime
        builtin_skills.set_skill_runtime(router, None)

        result = builtin_skills.invoke_skill("greet", "world")
        assert isinstance(result, dict)
        assert result["success"] is True
        assert "content" in result
        assert result["skill_id"] == "greet"
        assert result["skill_name"] == "greet"
        assert result["source"] == "project"
        assert result["execution_mode"] == "inline"

    def test_invoke_returns_dict_not_found(self):
        from agent_framework.tools import builtin_skills

        router = SkillRouter()
        builtin_skills.set_skill_runtime(router, None)

        result = builtin_skills.invoke_skill("nonexistent")
        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error" in result

    def test_invoke_returns_dict_not_initialized(self):
        from agent_framework.tools import builtin_skills

        builtin_skills._skill_router = None
        result = builtin_skills.invoke_skill("anything")
        assert isinstance(result, dict)
        assert result["success"] is False

    def test_invoke_config_skill_returns_dict(self):
        from agent_framework.tools import builtin_skills

        router = SkillRouter()
        router.register_skill(Skill(
            skill_id="cfg-skill",
            name="Config Skill",
            system_prompt_addon="Do config things.",
        ))
        builtin_skills.set_skill_runtime(router, None)

        result = builtin_skills.invoke_skill("cfg-skill", "arg1")
        assert isinstance(result, dict)
        assert result["success"] is True
        assert "Do config things" in result["content"]
        assert result["skill_id"] == "cfg-skill"


# =====================================================================
# 6. Backward compat: discover_skills() still works
# =====================================================================

class TestDiscoverSkillsBackwardCompat:

    def test_discover_skills_returns_list(self, tmp_path: Path):
        d = tmp_path / "skills"
        _write_skill(d, "alpha", description="first skill")
        _write_skill(d, "beta", description="second skill")

        result = discover_skills([d])
        assert isinstance(result, list)
        assert len(result) == 2
        ids = {r["skill_id"] for r in result}
        assert ids == {"alpha", "beta"}

    def test_discover_skills_dedup(self, tmp_path: Path):
        d1 = tmp_path / "d1"
        d2 = tmp_path / "d2"
        _write_skill(d1, "same", description="from d1")
        _write_skill(d2, "same", description="from d2")

        # Legacy mode: same priority, last wins
        result = discover_skills([d1, d2])
        assert len(result) == 1

    def test_discover_skills_skips_missing_dir(self, tmp_path: Path):
        missing = tmp_path / "does_not_exist"
        result = discover_skills([missing])
        assert result == []

    def test_load_file_skills_still_works(self, tmp_path: Path):
        """SkillRouter.load_file_skills backward compat."""
        d = tmp_path / "skills"
        _write_skill(d, "legacy-skill")

        router = SkillRouter()
        count = router.load_file_skills([d])
        assert count == 1
        assert router.get_skill("legacy-skill") is not None

"""Tests for file-based skill system (SKILL.md)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent_framework.models.agent import Skill


# =====================================================================
# SKILL.md Parsing
# =====================================================================


class TestSkillMdParsing:

    def test_parse_with_frontmatter(self, tmp_path):
        from agent_framework.skills.loader import parse_skill_md
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: my-skill
            description: Does something useful
            ---

            You are a helpful assistant. $ARGUMENTS
        """))
        result = parse_skill_md(skill_file)
        assert result is not None
        assert result["frontmatter"]["name"] == "my-skill"
        assert result["frontmatter"]["description"] == "Does something useful"
        assert "$ARGUMENTS" in result["body"]

    def test_parse_without_frontmatter(self, tmp_path):
        from agent_framework.skills.loader import parse_skill_md
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text("Just a plain prompt body.\n")
        result = parse_skill_md(skill_file)
        assert result is not None
        assert result["frontmatter"] == {}
        assert "plain prompt" in result["body"]

    def test_parse_boolean_fields(self, tmp_path):
        from agent_framework.skills.loader import parse_skill_md
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: safe-skill
            disable-model-invocation: true
            user-invocable: false
            ---

            Body here.
        """))
        result = parse_skill_md(skill_file)
        fm = result["frontmatter"]
        assert fm["disable-model-invocation"] is True
        assert fm["user-invocable"] is False

    def test_parse_list_fields(self, tmp_path):
        from agent_framework.skills.loader import parse_skill_md
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: tool-skill
            allowed-tools:
              - read_file
              - run_command
            ---

            Use the tools.
        """))
        result = parse_skill_md(skill_file)
        assert result["frontmatter"]["allowed-tools"] == ["read_file", "run_command"]

    def test_parse_nonexistent_returns_none(self, tmp_path):
        from agent_framework.skills.loader import parse_skill_md
        result = parse_skill_md(tmp_path / "nope.md")
        assert result is None

    def test_parse_argument_hint(self, tmp_path):
        from agent_framework.skills.loader import parse_skill_md
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: deploy
            argument-hint: "[env] [version]"
            ---

            Deploy $0 version $1.
        """))
        result = parse_skill_md(skill_file)
        assert result["frontmatter"]["argument-hint"] == "[env] [version]"


# =====================================================================
# Directory Discovery
# =====================================================================


class TestSkillDiscovery:

    def test_discover_directory_layout(self, tmp_path):
        from agent_framework.skills.loader import discover_skills
        # skills/my-skill/SKILL.md
        skill_dir = tmp_path / "my-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: my-skill
            description: Test skill
            ---

            Do the thing.
        """))
        results = discover_skills([tmp_path])
        assert len(results) == 1
        assert results[0]["skill_id"] == "my-skill"

    def test_discover_flat_md_layout(self, tmp_path):
        from agent_framework.skills.loader import discover_skills
        # skills/helper.md
        (tmp_path / "helper.md").write_text(textwrap.dedent("""\
            ---
            name: helper
            description: A flat skill
            ---

            Help the user.
        """))
        results = discover_skills([tmp_path])
        assert len(results) == 1
        assert results[0]["skill_id"] == "helper"

    def test_discover_multiple_directories(self, tmp_path):
        from agent_framework.skills.loader import discover_skills
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        skill_a = dir_a / "skill-a"
        skill_a.mkdir()
        (skill_a / "SKILL.md").write_text("---\nname: a\n---\nA")

        dir_b = tmp_path / "b"
        dir_b.mkdir()
        skill_b = dir_b / "skill-b"
        skill_b.mkdir()
        (skill_b / "SKILL.md").write_text("---\nname: b\n---\nB")

        results = discover_skills([dir_a, dir_b])
        ids = {r["skill_id"] for r in results}
        assert ids == {"a", "b"}

    def test_discover_dedup_by_id(self, tmp_path):
        from agent_framework.skills.loader import discover_skills
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        sa = dir_a / "dup"
        sa.mkdir()
        (sa / "SKILL.md").write_text("---\nname: dup\n---\nFirst")

        dir_b = tmp_path / "b"
        dir_b.mkdir()
        sb = dir_b / "dup"
        sb.mkdir()
        (sb / "SKILL.md").write_text("---\nname: dup\n---\nSecond")

        results = discover_skills([dir_a, dir_b])
        assert len(results) == 1  # first wins

    def test_discover_empty_directory(self, tmp_path):
        from agent_framework.skills.loader import discover_skills
        results = discover_skills([tmp_path])
        assert results == []

    def test_discover_nonexistent_directory(self):
        from agent_framework.skills.loader import discover_skills
        results = discover_skills([Path("/nonexistent/path")])
        assert results == []

    def test_discover_name_from_directory(self, tmp_path):
        from agent_framework.skills.loader import discover_skills
        skill_dir = tmp_path / "auto-name"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\ndescription: no name field\n---\nBody")
        results = discover_skills([tmp_path])
        assert results[0]["skill_id"] == "auto-name"


# =====================================================================
# Lazy Loading
# =====================================================================


class TestLazyLoading:

    def test_load_skill_body(self, tmp_path):
        from agent_framework.skills.loader import load_skill_body
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: x\n---\n\nThe actual body content.")
        body = load_skill_body(f)
        assert body == "The actual body content."

    def test_load_body_without_frontmatter(self, tmp_path):
        from agent_framework.skills.loader import load_skill_body
        f = tmp_path / "plain.md"
        f.write_text("Just raw instructions.")
        body = load_skill_body(f)
        assert body == "Just raw instructions."

    def test_load_nonexistent_raises(self):
        from agent_framework.skills.loader import load_skill_body
        with pytest.raises(FileNotFoundError):
            load_skill_body("/nonexistent/SKILL.md")


# =====================================================================
# Argument Substitution
# =====================================================================


class TestArgumentSubstitution:

    def test_substitute_arguments_placeholder(self):
        from agent_framework.skills.preprocessor import substitute_arguments
        body = "Do this: $ARGUMENTS"
        result = substitute_arguments(body, "hello world")
        assert result == "Do this: hello world"

    def test_substitute_positional(self):
        from agent_framework.skills.preprocessor import substitute_arguments
        body = "Deploy $0 version $1"
        result = substitute_arguments(body, "staging 2.0")
        assert result == "Deploy staging version 2.0"

    def test_substitute_no_placeholder_appends(self):
        from agent_framework.skills.preprocessor import substitute_arguments
        body = "Do the task."
        result = substitute_arguments(body, "some args")
        assert "ARGUMENTS: some args" in result

    def test_substitute_empty_args(self):
        from agent_framework.skills.preprocessor import substitute_arguments
        body = "Do $ARGUMENTS"
        result = substitute_arguments(body, "")
        assert "$ARGUMENTS" not in result

    def test_substitute_unused_positional_cleaned(self):
        from agent_framework.skills.preprocessor import substitute_arguments
        body = "Use $0 and $1 and $2"
        result = substitute_arguments(body, "only-one")
        assert "only-one" in result
        assert "$1" not in result
        assert "$2" not in result


# =====================================================================
# Shell Preprocessing
# =====================================================================


class TestShellPreprocessing:

    def test_shell_directive_echo(self):
        from agent_framework.skills.preprocessor import execute_shell_directives
        body = "Result: !`echo hello`"
        result = execute_shell_directives(body)
        assert "hello" in result
        assert "!`" not in result

    def test_shell_directive_timeout(self):
        from agent_framework.skills.preprocessor import execute_shell_directives
        body = "Data: !`sleep 30`"
        result = execute_shell_directives(body)
        assert "timed out" in result

    def test_shell_directive_failure(self):
        from agent_framework.skills.preprocessor import execute_shell_directives
        body = "Data: !`nonexistent_command_xyz 2>/dev/null`"
        result = execute_shell_directives(body)
        # Should contain fallback text, not crash
        assert "!`" not in result

    def test_no_shell_directives_passthrough(self):
        from agent_framework.skills.preprocessor import execute_shell_directives
        body = "No commands here."
        result = execute_shell_directives(body)
        assert result == body

    def test_full_preprocess_pipeline(self):
        from agent_framework.skills.preprocessor import preprocess_skill
        body = "Deploy $ARGUMENTS. Status: !`echo ok`"
        result = preprocess_skill(body, raw_args="prod", enable_shell=True)
        assert "Deploy prod" in result
        assert "ok" in result

    def test_preprocess_shell_disabled(self):
        from agent_framework.skills.preprocessor import preprocess_skill
        body = "Data: !`echo secret`"
        result = preprocess_skill(body, enable_shell=False)
        assert "!`echo secret`" in result  # Not executed


# =====================================================================
# SkillRouter File Loading
# =====================================================================


class TestSkillRouterFileLoading:

    def test_load_file_skills(self, tmp_path):
        from agent_framework.agent.skill_router import SkillRouter
        skill_dir = tmp_path / "test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: test-skill
            description: A test skill
            argument-hint: "[arg]"
            ---

            Do the test thing.
        """))
        router = SkillRouter()
        count = router.load_file_skills([tmp_path])
        assert count == 1
        skill = router.get_skill("test-skill")
        assert skill is not None
        assert skill.description == "A test skill"
        assert skill.source_path is not None
        assert skill.argument_hint == "[arg]"

    def test_file_skills_coexist_with_config_skills(self, tmp_path):
        from agent_framework.agent.skill_router import SkillRouter
        router = SkillRouter()
        # Config-based
        router.register_skill(Skill(
            skill_id="config-skill", name="Config", description="From config",
            trigger_keywords=["keyword"], system_prompt_addon="addon",
        ))
        # File-based
        skill_dir = tmp_path / "file-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: file-skill\ndescription: From file\n---\nBody")
        router.load_file_skills([tmp_path])

        assert len(router.list_skills()) == 2
        assert router.get_skill("config-skill") is not None
        assert router.get_skill("file-skill") is not None

    def test_get_skill_descriptions_excludes_disabled(self, tmp_path):
        from agent_framework.agent.skill_router import SkillRouter
        router = SkillRouter()
        router.register_skill(Skill(
            skill_id="visible", description="I'm visible",
        ))
        router.register_skill(Skill(
            skill_id="hidden", description="I'm hidden",
            disable_model_invocation=True,
        ))
        descs = router.get_skill_descriptions()
        ids = {d["skill_id"] for d in descs}
        assert "visible" in ids
        assert "hidden" not in ids

    def test_get_skill_descriptions_skips_no_description(self):
        from agent_framework.agent.skill_router import SkillRouter
        router = SkillRouter()
        router.register_skill(Skill(skill_id="empty"))
        descs = router.get_skill_descriptions()
        assert len(descs) == 0

    def test_keyword_detection_still_works(self):
        """Backward compatibility: keyword skills still trigger."""
        from agent_framework.agent.skill_router import SkillRouter
        router = SkillRouter()
        router.register_skill(Skill(
            skill_id="math", trigger_keywords=["calculate"],
            system_prompt_addon="math mode",
        ))
        detected = router.detect_skill("please calculate 2+2")
        assert detected is not None
        assert detected.skill_id == "math"

    def test_file_skills_not_detected_by_keywords(self, tmp_path):
        """File-based skills without keywords are not keyword-detected."""
        from agent_framework.agent.skill_router import SkillRouter
        router = SkillRouter()
        skill_dir = tmp_path / "no-kw"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: no-kw\ndescription: No keywords\n---\nBody")
        router.load_file_skills([tmp_path])
        assert router.detect_skill("no-kw something") is None


# =====================================================================
# invoke_skill Tool
# =====================================================================


class TestInvokeSkillTool:

    def test_invoke_file_skill(self, tmp_path):
        from agent_framework.agent.skill_router import SkillRouter
        from agent_framework.tools.builtin_skills import invoke_skill, set_skill_runtime

        skill_dir = tmp_path / "greet"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: greet
            description: Greet someone
            ---

            Say hello to $ARGUMENTS politely.
        """))
        router = SkillRouter()
        router.load_file_skills([tmp_path])
        set_skill_runtime(router, MagicMock())

        result = invoke_skill("greet", arguments="Alice")
        assert "Alice" in result
        assert "politely" in result

    def test_invoke_config_skill(self):
        from agent_framework.agent.skill_router import SkillRouter
        from agent_framework.tools.builtin_skills import invoke_skill, set_skill_runtime

        router = SkillRouter()
        router.register_skill(Skill(
            skill_id="translate", system_prompt_addon="You are a translator.",
        ))
        set_skill_runtime(router, MagicMock())

        result = invoke_skill("translate", arguments="Hello World")
        assert "translator" in result
        assert "Hello World" in result

    def test_invoke_nonexistent_skill(self):
        from agent_framework.agent.skill_router import SkillRouter
        from agent_framework.tools.builtin_skills import invoke_skill, set_skill_runtime

        router = SkillRouter()
        set_skill_runtime(router, MagicMock())
        result = invoke_skill("nonexistent")
        assert "ERROR" in result
        assert "not found" in result

    def test_invoke_not_initialized(self):
        from agent_framework.tools.builtin_skills import invoke_skill, set_skill_runtime
        set_skill_runtime(None, None)
        result = invoke_skill("anything")
        assert "not initialized" in result


# =====================================================================
# Context Injection
# =====================================================================


class TestSkillContextInjection:

    def test_collect_skill_catalog(self):
        from agent_framework.context.source_provider import ContextSourceProvider
        provider = ContextSourceProvider()
        descs = [
            {"skill_id": "commit", "name": "commit", "description": "Make a git commit"},
            {"skill_id": "review", "name": "review", "description": "Review code", "argument_hint": "[file]"},
        ]
        result = provider.collect_skill_catalog(descs)
        assert result is not None
        assert "invoke_skill" in result
        assert "commit" in result
        assert "review" in result
        assert "[file]" in result

    def test_collect_skill_catalog_empty(self):
        from agent_framework.context.source_provider import ContextSourceProvider
        provider = ContextSourceProvider()
        assert provider.collect_skill_catalog([]) is None

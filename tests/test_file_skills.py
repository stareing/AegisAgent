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


# =====================================================================
# Complex Skill Support (skill-creator style)
# =====================================================================


class TestComplexSkillSupport:
    """Tests for skill-creator-level features: ${SKILL_DIR}, companion files, cwd."""

    def test_skill_dir_substitution(self, tmp_path):
        from agent_framework.skills.preprocessor import preprocess_skill
        body = "Read ${SKILL_DIR}/agents/grader.md for grading instructions."
        result = preprocess_skill(body, skill_dir=str(tmp_path))
        assert str(tmp_path) in result
        assert "${SKILL_DIR}" not in result

    def test_claude_skill_dir_alias(self, tmp_path):
        from agent_framework.skills.preprocessor import preprocess_skill
        body = "Script at ${CLAUDE_SKILL_DIR}/scripts/run_eval.py"
        result = preprocess_skill(body, skill_dir=str(tmp_path))
        assert str(tmp_path) in result

    def test_skill_dir_with_arguments(self, tmp_path):
        from agent_framework.skills.preprocessor import preprocess_skill
        body = "Run ${SKILL_DIR}/scripts/test.py on $ARGUMENTS"
        result = preprocess_skill(body, raw_args="my-skill", skill_dir=str(tmp_path))
        assert str(tmp_path) in result
        assert "my-skill" in result

    def test_shell_directive_uses_skill_dir_as_cwd(self, tmp_path):
        from agent_framework.skills.preprocessor import preprocess_skill
        # Create a file in the skill directory
        (tmp_path / "marker.txt").write_text("FOUND_IT")
        body = "Marker: !`cat marker.txt`"
        result = preprocess_skill(body, skill_dir=str(tmp_path))
        assert "FOUND_IT" in result

    def test_load_supporting_file(self, tmp_path):
        from agent_framework.skills.loader import load_supporting_file
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        (agents_dir / "grader.md").write_text("You are a grader.")
        content = load_supporting_file(tmp_path, "agents/grader.md")
        assert "grader" in content

    def test_load_supporting_file_not_found(self, tmp_path):
        from agent_framework.skills.loader import load_supporting_file
        with pytest.raises(FileNotFoundError):
            load_supporting_file(tmp_path, "agents/nonexistent.md")

    def test_load_supporting_file_path_traversal_blocked(self, tmp_path):
        from agent_framework.skills.loader import load_supporting_file
        with pytest.raises(ValueError, match="traversal"):
            load_supporting_file(tmp_path, "../../etc/passwd")

    def test_list_skill_files(self, tmp_path):
        from agent_framework.skills.loader import list_skill_files
        (tmp_path / "SKILL.md").write_text("---\nname: x\n---\nBody")
        agents = tmp_path / "agents"
        agents.mkdir()
        (agents / "grader.md").write_text("grade")
        scripts = tmp_path / "scripts"
        scripts.mkdir()
        (scripts / "run.py").write_text("run")

        files = list_skill_files(tmp_path)
        assert "SKILL.md" in files
        assert "agents/grader.md" in files
        assert "scripts/run.py" in files

    def test_invoke_skill_with_skill_dir(self, tmp_path):
        """invoke_skill passes skill_dir correctly for ${SKILL_DIR} resolution."""
        from agent_framework.agent.skill_router import SkillRouter
        from agent_framework.tools.builtin_skills import invoke_skill, set_skill_runtime

        skill_dir = tmp_path / "my-complex-skill"
        skill_dir.mkdir()
        refs = skill_dir / "references"
        refs.mkdir()
        (refs / "schema.md").write_text("Schema definition here")
        (skill_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: my-complex-skill
            description: A complex skill with supporting files
            ---

            Read schema from ${SKILL_DIR}/references/schema.md
            Task: $ARGUMENTS
        """))

        router = SkillRouter()
        router.load_file_skills([tmp_path])
        set_skill_runtime(router, MagicMock())

        result = invoke_skill("my-complex-skill", arguments="do the thing")
        # ${SKILL_DIR} should be resolved to actual path
        assert str(skill_dir) in result
        assert "do the thing" in result
        assert "${SKILL_DIR}" not in result

    def test_full_complex_skill_structure(self, tmp_path):
        """Test a skill with structure mirroring skill-creator."""
        from agent_framework.skills.loader import (
            discover_skills,
            list_skill_files,
            load_skill_body,
            load_supporting_file,
        )
        from agent_framework.skills.preprocessor import preprocess_skill

        # Build a skill-creator-like structure
        skill = tmp_path / "my-skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: my-skill
            description: Create and test skills
            ---

            Use grader at ${SKILL_DIR}/agents/grader.md
            Run tests: !`echo "3 tests passed"`
            User request: $ARGUMENTS
        """))
        (skill / "agents").mkdir()
        (skill / "agents" / "grader.md").write_text("You are a grader. Score 1-5.")
        (skill / "scripts").mkdir()
        (skill / "scripts" / "run_eval.py").write_text("print('eval')")
        (skill / "references").mkdir()
        (skill / "references" / "schemas.md").write_text("{ schema: ... }")

        # Discovery
        results = discover_skills([tmp_path])
        assert len(results) == 1
        assert results[0]["skill_id"] == "my-skill"

        # List files
        files = list_skill_files(skill)
        assert "agents/grader.md" in files
        assert "scripts/run_eval.py" in files
        assert "references/schemas.md" in files

        # Load body
        body = load_skill_body(skill / "SKILL.md")
        assert "${SKILL_DIR}" in body  # Not yet preprocessed

        # Preprocess
        processed = preprocess_skill(
            body, raw_args="build my-feature", skill_dir=str(skill)
        )
        assert str(skill) in processed  # ${SKILL_DIR} resolved
        assert "3 tests passed" in processed  # shell executed
        assert "build my-feature" in processed  # args substituted

        # Read supporting file
        grader = load_supporting_file(skill, "agents/grader.md")
        assert "grader" in grader
        assert "Score 1-5" in grader

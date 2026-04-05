"""Tests for Phase 1: Agent Definition System + Plan Mode (v4.0)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from agent_framework.models.agent import (
    AgentDefinition,
    ApprovalMode,
    PlanModeState,
)


# =====================================================================
# Shared Frontmatter Parser (infra/frontmatter.py)
# =====================================================================


class TestFrontmatterParser:

    def test_mini_yaml_parse_scalars(self):
        from agent_framework.infra.frontmatter import mini_yaml_parse
        result = mini_yaml_parse("name: my-agent\ndescription: A helpful agent")
        assert result["name"] == "my-agent"
        assert result["description"] == "A helpful agent"

    def test_mini_yaml_parse_booleans(self):
        from agent_framework.infra.frontmatter import mini_yaml_parse
        result = mini_yaml_parse("enabled: true\ndisabled: false\nyes_val: yes")
        assert result["enabled"] is True
        assert result["disabled"] is False
        assert result["yes_val"] is True

    def test_mini_yaml_parse_nulls(self):
        from agent_framework.infra.frontmatter import mini_yaml_parse
        result = mini_yaml_parse("val1: null\nval2: none\nval3: ~")
        assert result["val1"] is None
        assert result["val2"] is None
        assert result["val3"] is None

    def test_mini_yaml_parse_lists(self):
        from agent_framework.infra.frontmatter import mini_yaml_parse
        result = mini_yaml_parse("tools:\n- read_file\n- glob_files\n- grep_search")
        assert result["tools"] == ["read_file", "glob_files", "grep_search"]

    def test_mini_yaml_parse_comments_ignored(self):
        from agent_framework.infra.frontmatter import mini_yaml_parse
        result = mini_yaml_parse("# comment\nname: test\n# another comment")
        assert result == {"name": "test"}

    def test_parse_frontmatter_file(self, tmp_path):
        from agent_framework.infra.frontmatter import parse_frontmatter_file
        md_file = tmp_path / "test.md"
        md_file.write_text(textwrap.dedent("""\
            ---
            name: test-agent
            description: A test agent
            ---

            System instructions here.
        """))
        result = parse_frontmatter_file(md_file)
        assert result is not None
        assert result["frontmatter"]["name"] == "test-agent"
        assert "System instructions here." in result["body"]

    def test_parse_frontmatter_file_no_frontmatter(self, tmp_path):
        from agent_framework.infra.frontmatter import parse_frontmatter_file
        md_file = tmp_path / "bare.md"
        md_file.write_text("Just a body with no frontmatter.")
        result = parse_frontmatter_file(md_file)
        assert result is not None
        assert result["frontmatter"] == {}
        assert result["body"] == "Just a body with no frontmatter."

    def test_parse_frontmatter_file_missing(self, tmp_path):
        from agent_framework.infra.frontmatter import parse_frontmatter_file
        result = parse_frontmatter_file(tmp_path / "nonexistent.md")
        assert result is None

    def test_backward_compat_with_skill_loader(self, tmp_path):
        """Verify skill loader still works after extraction."""
        from agent_framework.skills.loader import parse_skill_md
        skill_file = tmp_path / "SKILL.md"
        skill_file.write_text(textwrap.dedent("""\
            ---
            name: compat-test
            description: Backward compatibility
            ---

            Skill body.
        """))
        result = parse_skill_md(skill_file)
        assert result is not None
        assert result["frontmatter"]["name"] == "compat-test"


# =====================================================================
# Agent Definition Model
# =====================================================================


class TestAgentDefinitionModel:

    def test_frozen(self):
        defn = AgentDefinition(definition_id="test", name="Test")
        with pytest.raises(Exception):
            defn.name = "changed"

    def test_defaults(self):
        defn = AgentDefinition(definition_id="test")
        assert defn.agent_type == "general"
        assert defn.source == "project"
        assert defn.tools is None
        assert defn.disallowed_tools == []
        assert defn.permission_mode == "default"
        assert defn.model is None
        assert defn.system_instructions == ""

    def test_with_tools(self):
        defn = AgentDefinition(
            definition_id="explore",
            tools=["read_file", "grep_search"],
            disallowed_tools=["bash_exec"],
        )
        assert defn.tools == ["read_file", "grep_search"]
        assert "bash_exec" in defn.disallowed_tools


# =====================================================================
# Agent Definition Loader
# =====================================================================


class TestAgentDefinitionLoader:

    def test_load_builtins(self):
        from agent_framework.agent.definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader(load_builtins=True)
        defs = loader.load_all()
        assert len(defs) >= 4
        assert "general-purpose" in defs
        assert "explore" in defs
        assert "plan" in defs
        assert "verification" in defs

    def test_builtin_general_has_no_tool_restriction(self):
        from agent_framework.agent.definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader(load_builtins=True)
        defs = loader.load_all()
        general = defs["general-purpose"]
        assert general.tools is None  # all tools available
        assert general.agent_type == "general"
        assert general.source == "builtin"

    def test_builtin_explore_has_tool_whitelist(self):
        from agent_framework.agent.definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader(load_builtins=True)
        defs = loader.load_all()
        explore = defs["explore"]
        assert explore.tools is not None
        assert "read_file" in explore.tools
        assert "grep_search" in explore.tools

    def test_builtin_plan_has_plan_permission(self):
        from agent_framework.agent.definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader(load_builtins=True)
        defs = loader.load_all()
        plan = defs["plan"]
        assert plan.permission_mode == "plan"

    def test_load_from_project_directory(self, tmp_path):
        from agent_framework.agent.definition import AgentDefinitionLoader
        agents_dir = tmp_path / ".agent_framework" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "custom.md").write_text(textwrap.dedent("""\
            ---
            name: custom-agent
            description: Custom project agent
            agent_type: custom
            tools:
              - read_file
              - bash_exec
            ---

            Custom instructions.
        """))
        loader = AgentDefinitionLoader(
            project_root=tmp_path,
            load_builtins=False,
        )
        defs = loader.load_all()
        assert "custom-agent" in defs
        assert defs["custom-agent"].tools == ["read_file", "bash_exec"]
        assert defs["custom-agent"].source == "project"

    def test_project_overrides_builtin(self, tmp_path):
        from agent_framework.agent.definition import AgentDefinitionLoader
        agents_dir = tmp_path / ".agent_framework" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "explore.md").write_text(textwrap.dedent("""\
            ---
            name: explore
            description: Overridden explore agent
            agent_type: explore
            ---

            Custom explore instructions.
        """))
        loader = AgentDefinitionLoader(
            project_root=tmp_path,
            load_builtins=True,
        )
        defs = loader.load_all()
        assert defs["explore"].description == "Overridden explore agent"
        assert defs["explore"].source == "project"

    def test_get_definition(self):
        from agent_framework.agent.definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader(load_builtins=True)
        loader.load_all()
        result = loader.get("explore")
        assert result is not None
        assert result.definition_id == "explore"

    def test_get_nonexistent(self):
        from agent_framework.agent.definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader(load_builtins=True)
        loader.load_all()
        assert loader.get("nonexistent") is None

    def test_list_by_type(self):
        from agent_framework.agent.definition import AgentDefinitionLoader
        loader = AgentDefinitionLoader(load_builtins=True)
        loader.load_all()
        plans = loader.list_by_type("plan")
        assert len(plans) >= 1
        assert all(d.agent_type == "plan" for d in plans)

    def test_extra_directories(self, tmp_path):
        from agent_framework.agent.definition import AgentDefinitionLoader
        extra_dir = tmp_path / "extra_agents"
        extra_dir.mkdir()
        (extra_dir / "policy-agent.md").write_text(textwrap.dedent("""\
            ---
            name: policy-agent
            description: Policy managed agent
            ---

            Policy instructions.
        """))
        loader = AgentDefinitionLoader(
            extra_directories=[str(extra_dir)],
            load_builtins=False,
        )
        defs = loader.load_all()
        assert "policy-agent" in defs
        assert defs["policy-agent"].source == "policy"

    def test_directory_per_agent_layout(self, tmp_path):
        from agent_framework.agent.definition import AgentDefinitionLoader
        agents_dir = tmp_path / ".agent_framework" / "agents"
        agent_subdir = agents_dir / "my-agent"
        agent_subdir.mkdir(parents=True)
        (agent_subdir / "agent.md").write_text(textwrap.dedent("""\
            ---
            name: my-agent
            description: Directory-based agent
            ---

            Instructions.
        """))
        loader = AgentDefinitionLoader(
            project_root=tmp_path,
            load_builtins=False,
        )
        defs = loader.load_all()
        assert "my-agent" in defs


# =====================================================================
# Capability Policy with Agent Definition
# =====================================================================


class TestCapabilityPolicyWithDefinition:

    def _make_tool_entry(self, name: str, category: str = "general"):
        from agent_framework.models.tool import ToolEntry, ToolMeta
        from unittest.mock import MagicMock
        entry = MagicMock(spec=ToolEntry)
        entry.meta = ToolMeta(name=name, category=category)
        return entry

    def test_definition_whitelist_filters_tools(self):
        from agent_framework.agent.capability_policy import apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        tools = [
            self._make_tool_entry("read_file", "filesystem_read"),
            self._make_tool_entry("write_file", "filesystem_write"),
            self._make_tool_entry("bash_exec", "shell"),
        ]
        defn = AgentDefinition(
            definition_id="test",
            tools=["read_file"],
        )
        result = apply_capability_policy(
            tools, CapabilityPolicy(), agent_definition=defn,
        )
        assert len(result) == 1
        assert result[0].meta.name == "read_file"

    def test_definition_blocklist_removes_tools(self):
        from agent_framework.agent.capability_policy import apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        tools = [
            self._make_tool_entry("read_file", "filesystem_read"),
            self._make_tool_entry("bash_exec", "shell"),
        ]
        defn = AgentDefinition(
            definition_id="test",
            disallowed_tools=["bash_exec"],
        )
        result = apply_capability_policy(
            tools, CapabilityPolicy(), agent_definition=defn,
        )
        assert len(result) == 1
        assert result[0].meta.name == "read_file"

    def test_no_definition_passes_all_tools(self):
        from agent_framework.agent.capability_policy import apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        tools = [
            self._make_tool_entry("read_file"),
            self._make_tool_entry("bash_exec"),
        ]
        result = apply_capability_policy(
            tools, CapabilityPolicy(), agent_definition=None,
        )
        assert len(result) == 2


# =====================================================================
# Plan Mode State
# =====================================================================


class TestPlanModeState:

    def test_defaults(self):
        state = PlanModeState()
        assert state.active is False
        assert state.pre_plan_approval_mode == ApprovalMode.DEFAULT
        assert state.plan_file_path is None
        assert state.plan_slug == ""


# =====================================================================
# Plan Mode Controller
# =====================================================================


class TestPlanModeController:

    def test_enter_plan(self, tmp_path):
        from agent_framework.agent.plan_mode import PlanModeController
        ctrl = PlanModeController(plan_dir=str(tmp_path / "plans"))
        state = ctrl.enter_plan(ApprovalMode.DEFAULT, task="Implement auth")
        assert state.active is True
        assert state.pre_plan_approval_mode == ApprovalMode.DEFAULT
        assert state.plan_file_path is not None
        assert "Implement-auth" in state.plan_slug or "Implement" in state.plan_slug

    def test_exit_plan_restores_mode(self, tmp_path):
        from agent_framework.agent.plan_mode import PlanModeController
        ctrl = PlanModeController(plan_dir=str(tmp_path / "plans"))
        state = ctrl.enter_plan(ApprovalMode.AUTO_EDIT, task="test")
        restored = ctrl.exit_plan(state)
        assert restored == ApprovalMode.AUTO_EDIT

    def test_exit_plan_not_active_raises(self):
        from agent_framework.agent.plan_mode import PlanModeController
        ctrl = PlanModeController()
        with pytest.raises(ValueError, match="not active"):
            ctrl.exit_plan(PlanModeState())

    def test_write_plan(self, tmp_path):
        from agent_framework.agent.plan_mode import PlanModeController
        ctrl = PlanModeController(plan_dir=str(tmp_path / "plans"))
        state = ctrl.enter_plan(ApprovalMode.DEFAULT, task="test")
        path = ctrl.write_plan(state, "# My Plan\n\nStep 1...")
        assert Path(path).is_file()
        assert "# My Plan" in Path(path).read_text()

    def test_read_plan(self, tmp_path):
        from agent_framework.agent.plan_mode import PlanModeController
        ctrl = PlanModeController(plan_dir=str(tmp_path / "plans"))
        state = ctrl.enter_plan(ApprovalMode.DEFAULT, task="test")
        ctrl.write_plan(state, "Plan content")
        content = ctrl.read_plan(state)
        assert content == "Plan content"

    def test_read_plan_nonexistent(self, tmp_path):
        from agent_framework.agent.plan_mode import PlanModeController
        ctrl = PlanModeController(plan_dir=str(tmp_path / "plans"))
        state = ctrl.enter_plan(ApprovalMode.DEFAULT, task="test")
        # Don't write anything
        content = ctrl.read_plan(state)
        assert content is None


# =====================================================================
# New HookPoints
# =====================================================================


class TestNewHookPoints:

    def test_hook_points_exist(self):
        from agent_framework.models.hook import HookPoint
        assert HookPoint.AGENT_DEFINITION_LOADED == "agent_definition.loaded"
        assert HookPoint.PLAN_MODE_ENTERED == "plan_mode.entered"
        assert HookPoint.PLAN_MODE_EXITED == "plan_mode.exited"
        assert HookPoint.WORKTREE_ENTERED == "worktree.entered"
        assert HookPoint.WORKTREE_EXITED == "worktree.exited"


# =====================================================================
# New ErrorCodes
# =====================================================================


class TestNewErrorCodes:

    def test_error_codes_exist(self):
        from agent_framework.models.tool import ErrorCode
        assert ErrorCode.WORKTREE_FAILED == "WORKTREE_FAILED"
        assert ErrorCode.PLAN_MODE_VIOLATION == "PLAN_MODE_VIOLATION"
        assert ErrorCode.DEFERRED_TOOL_NOT_FOUND == "DEFERRED_TOOL_NOT_FOUND"


# =====================================================================
# New ToolMeta fields
# =====================================================================


class TestToolMetaExtensions:

    def test_should_defer_default(self):
        from agent_framework.models.tool import ToolMeta
        meta = ToolMeta(name="test")
        assert meta.should_defer is False

    def test_concurrency_class_default(self):
        from agent_framework.models.tool import ToolMeta
        meta = ToolMeta(name="test")
        assert meta.concurrency_class == "non_concurrent"

    def test_concurrent_safe_tool(self):
        from agent_framework.models.tool import ToolMeta
        meta = ToolMeta(name="read_file", concurrency_class="concurrent_safe")
        assert meta.concurrency_class == "concurrent_safe"


# =====================================================================
# Enhanced Skill Model
# =====================================================================


class TestEnhancedSkillModel:

    def test_new_fields_have_defaults(self):
        from agent_framework.models.agent import Skill
        skill = Skill(skill_id="test")
        assert skill.execution_mode == "inline"
        assert skill.effort_level is None
        assert skill.hooks == {}
        assert skill.paths == []
        assert skill.arguments == []
        assert skill.skill_source == "project"
        assert skill.priority == 0

    def test_new_fields_settable(self):
        from agent_framework.models.agent import Skill
        skill = Skill(
            skill_id="advanced",
            execution_mode="fork",
            effort_level="extensive",
            hooks={"pre_run": "validate"},
            paths=["src/main.py"],
            arguments=[{"name": "target", "required": True}],
            skill_source="builtin",
            priority=10,
        )
        assert skill.execution_mode == "fork"
        assert skill.effort_level == "extensive"
        assert skill.priority == 10

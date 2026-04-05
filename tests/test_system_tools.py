"""Tests for system tools layer rewrite.

Covers:
1. Tool metadata: categories, namespaces, tags
2. Category mapping consistency
3. Sub-agent default policy
4. Capability policy integration with new categories
5. New tools: web_search, control_tools
6. Shell module extraction
7. Parameter schemas
"""

from __future__ import annotations

import pytest

from agent_framework.tools.schemas.builtin_args import (SYSTEM_NAMESPACE,
                                                        ToolCategory)

# ---------------------------------------------------------------------------
# Tool metadata validation
# ---------------------------------------------------------------------------

class TestToolMetadata:
    """Verify all builtin tools have correct metadata."""

    def _get_meta(self, func):
        return getattr(func, "__tool_meta__", None)

    def test_filesystem_tools_category(self) -> None:
        from agent_framework.tools.builtin.filesystem import (file_exists,
                                                              list_directory,
                                                              read_file,
                                                              write_file)
        for fn in (read_file, write_file, list_directory, file_exists):
            meta = self._get_meta(fn)
            assert meta is not None, f"{fn.__name__} has no __tool_meta__"
            assert meta.category == ToolCategory.FILESYSTEM
            assert meta.namespace == SYSTEM_NAMESPACE
            assert "system" in meta.tags

    def test_search_tools_category(self) -> None:
        from agent_framework.tools.builtin.search import (glob_files,
                                                          grep_search)
        for fn in (grep_search, glob_files):
            meta = self._get_meta(fn)
            assert meta.category == ToolCategory.FILESYSTEM
            assert "search" in meta.tags
            assert meta.namespace == SYSTEM_NAMESPACE

    def test_shell_tools_category(self) -> None:
        from agent_framework.tools.builtin.shell import (bash_exec,
                                                         bash_output,
                                                         bash_stop, kill_shell,
                                                         task_stop)
        for fn in (bash_exec, bash_output, bash_stop, task_stop, kill_shell):
            meta = self._get_meta(fn)
            assert meta.category == ToolCategory.SYSTEM
            assert "shell" in meta.tags
            assert meta.namespace == SYSTEM_NAMESPACE

    def test_shell_dangerous_tags(self) -> None:
        from agent_framework.tools.builtin.shell import bash_exec, kill_shell
        for fn in (bash_exec, kill_shell):
            meta = self._get_meta(fn)
            assert "dangerous" in meta.tags
            assert meta.require_confirm is True

    def test_web_tools_category(self) -> None:
        from agent_framework.tools.builtin.web import web_fetch, web_search
        for fn in (web_fetch, web_search):
            meta = self._get_meta(fn)
            assert meta.category == ToolCategory.NETWORK
            assert "network" in meta.tags
            assert meta.namespace == SYSTEM_NAMESPACE

    def test_system_tools_category(self) -> None:
        from agent_framework.tools.builtin.system import get_env, run_command
        for fn in (run_command, get_env):
            meta = self._get_meta(fn)
            assert meta.category == ToolCategory.SYSTEM
            assert meta.namespace == SYSTEM_NAMESPACE
            assert meta.require_confirm is True

    def test_delegation_tools_category(self) -> None:
        from agent_framework.tools.builtin.spawn_agent import (
            check_spawn_result, spawn_agent)
        for fn in (spawn_agent, check_spawn_result):
            meta = self._get_meta(fn)
            assert meta.category == ToolCategory.DELEGATION
            assert "delegation" in meta.tags
            assert meta.namespace == SYSTEM_NAMESPACE

    def test_control_tools_category(self) -> None:
        from agent_framework.tools.builtin.task_manager import (task_create,
                                                                task_get,
                                                                task_list,
                                                                task_update)
        for fn in (task_create, task_update, task_list, task_get):
            meta = self._get_meta(fn)
            assert meta.category == ToolCategory.CONTROL
            assert "control" in meta.tags
            assert meta.namespace == SYSTEM_NAMESPACE

    def test_memory_admin_tools_category(self) -> None:
        from agent_framework.tools.builtin.memory_admin import (clear_memories,
                                                                forget_memory,
                                                                list_memories)
        for fn in (list_memories, forget_memory, clear_memories):
            meta = self._get_meta(fn)
            assert meta.category == ToolCategory.MEMORY_ADMIN
            assert "memory" in meta.tags
            assert meta.namespace == SYSTEM_NAMESPACE

    def test_think_tool_category(self) -> None:
        from agent_framework.tools.builtin.think import think
        meta = self._get_meta(think)
        assert meta.category == ToolCategory.REASONING
        assert meta.namespace == SYSTEM_NAMESPACE

    def test_write_tools_require_confirm(self) -> None:
        from agent_framework.tools.builtin.code_edit import (edit_file,
                                                             notebook_edit)
        from agent_framework.tools.builtin.filesystem import write_file
        for fn in (write_file, edit_file, notebook_edit):
            meta = self._get_meta(fn)
            assert meta.require_confirm is True, f"{meta.name} should require confirm"

    def test_read_tools_no_confirm(self) -> None:
        from agent_framework.tools.builtin.filesystem import (file_exists,
                                                              list_directory,
                                                              read_file)
        from agent_framework.tools.builtin.search import (glob_files,
                                                          grep_search)
        for fn in (read_file, list_directory, file_exists, grep_search, glob_files):
            meta = self._get_meta(fn)
            assert meta.require_confirm is False, f"{meta.name} should not require confirm"


# ---------------------------------------------------------------------------
# ToolCategory constants
# ---------------------------------------------------------------------------

class TestToolCategory:
    def test_subagent_safe_categories(self) -> None:
        assert "filesystem" in ToolCategory.SUBAGENT_SAFE
        assert "reasoning" in ToolCategory.SUBAGENT_SAFE

    def test_subagent_blocked_categories(self) -> None:
        for cat in ("system", "network", "control", "delegation", "memory_admin"):
            assert cat in ToolCategory.SUBAGENT_BLOCKED

    def test_high_risk_categories(self) -> None:
        assert "system" in ToolCategory.HIGH_RISK
        assert "network" in ToolCategory.HIGH_RISK
        assert "delegation" in ToolCategory.HIGH_RISK

    def test_no_overlap_safe_blocked(self) -> None:
        overlap = ToolCategory.SUBAGENT_SAFE & ToolCategory.SUBAGENT_BLOCKED
        assert len(overlap) == 0


# ---------------------------------------------------------------------------
# Sub-agent default policy
# ---------------------------------------------------------------------------

class TestSubagentDefaultPolicy:
    def test_build_subagent_default_policy(self) -> None:
        from agent_framework.tools.builtin import build_subagent_default_policy
        policy = build_subagent_default_policy()
        assert policy.allow_network_tools is False
        assert policy.allow_system_tools is False
        assert policy.allow_spawn is False
        assert policy.allow_memory_admin is False
        assert "system" in policy.blocked_tool_categories
        assert "network" in policy.blocked_tool_categories
        assert "delegation" in policy.blocked_tool_categories

    def test_subagent_default_blocked_categories(self) -> None:
        from agent_framework.tools.builtin import \
            SUBAGENT_DEFAULT_BLOCKED_CATEGORIES
        assert "system" in SUBAGENT_DEFAULT_BLOCKED_CATEGORIES
        assert "network" in SUBAGENT_DEFAULT_BLOCKED_CATEGORIES
        assert "control" in SUBAGENT_DEFAULT_BLOCKED_CATEGORIES
        assert "delegation" in SUBAGENT_DEFAULT_BLOCKED_CATEGORIES
        assert "memory_admin" in SUBAGENT_DEFAULT_BLOCKED_CATEGORIES


# ---------------------------------------------------------------------------
# Capability policy with new categories
# ---------------------------------------------------------------------------

class TestCapabilityPolicyNewCategories:
    def test_delegation_category_blocked_by_allow_spawn(self) -> None:
        from agent_framework.agent.capability_policy import \
            apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        from agent_framework.models.tool import ToolEntry, ToolMeta

        tools = [
            ToolEntry(meta=ToolMeta(name="read_file", category="filesystem", source="local")),
            ToolEntry(meta=ToolMeta(name="spawn_agent", category="delegation", source="subagent")),
        ]
        policy = CapabilityPolicy(allow_spawn=False)
        filtered = apply_capability_policy(tools, policy)
        names = {t.meta.name for t in filtered}
        assert "read_file" in names
        assert "spawn_agent" not in names

    def test_network_category_blocked(self) -> None:
        from agent_framework.agent.capability_policy import \
            apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        from agent_framework.models.tool import ToolEntry, ToolMeta

        tools = [
            ToolEntry(meta=ToolMeta(name="web_fetch", category="network", source="local")),
            ToolEntry(meta=ToolMeta(name="read_file", category="filesystem", source="local")),
        ]
        policy = CapabilityPolicy(allow_network_tools=False)
        filtered = apply_capability_policy(tools, policy)
        names = {t.meta.name for t in filtered}
        assert "web_fetch" not in names
        assert "read_file" in names

    def test_blocked_categories_filter(self) -> None:
        from agent_framework.agent.capability_policy import \
            apply_capability_policy
        from agent_framework.models.agent import CapabilityPolicy
        from agent_framework.models.tool import ToolEntry, ToolMeta

        tools = [
            ToolEntry(meta=ToolMeta(name="read_file", category="filesystem", source="local")),
            ToolEntry(meta=ToolMeta(name="task_create", category="control", source="local")),
            ToolEntry(meta=ToolMeta(name="think", category="reasoning", source="local")),
        ]
        policy = CapabilityPolicy(blocked_tool_categories=["control"])
        filtered = apply_capability_policy(tools, policy)
        names = {t.meta.name for t in filtered}
        assert "task_create" not in names
        assert "read_file" in names
        assert "think" in names


# ---------------------------------------------------------------------------
# New tools: control_tools
# ---------------------------------------------------------------------------

class TestControlTools:
    def test_slash_command_no_handler(self) -> None:
        from agent_framework.tools.builtin.control_tools import (
            set_command_handler, slash_command)
        set_command_handler(None)
        result = slash_command("/help")
        assert result["success"] is False
        assert "No command handler" in result["output"]

    def test_slash_command_with_handler(self) -> None:
        from agent_framework.tools.builtin.control_tools import (
            set_command_handler, slash_command)
        set_command_handler(lambda cmd: f"Handled: {cmd}")
        result = slash_command("/test")
        assert result["success"] is True
        assert "Handled: /test" in result["output"]
        set_command_handler(None)  # cleanup

    def test_slash_command_handler_error(self) -> None:
        from agent_framework.tools.builtin.control_tools import (
            set_command_handler, slash_command)
        set_command_handler(lambda cmd: (_ for _ in ()).throw(ValueError("bad")))
        result = slash_command("/fail")
        assert result["success"] is False
        set_command_handler(None)

    def test_exit_plan_mode_inactive(self) -> None:
        from agent_framework.tools.builtin.control_tools import (
            exit_plan_mode, set_plan_mode)
        set_plan_mode(False)
        result = exit_plan_mode("my plan")
        assert result["success"] is False
        assert "not active" in result["message"]

    def test_exit_plan_mode_active(self) -> None:
        from agent_framework.tools.builtin.control_tools import (
            exit_plan_mode, set_plan_mode)
        captured = {}
        set_plan_mode(True, callback=lambda p: captured.update(plan=p))
        result = exit_plan_mode("my plan")
        assert result["success"] is True
        assert captured["plan"] == "my plan"
        set_plan_mode(False)  # cleanup

    def test_control_tools_metadata(self) -> None:
        from agent_framework.tools.builtin.control_tools import (
            exit_plan_mode, slash_command)
        for fn in (slash_command, exit_plan_mode):
            meta = getattr(fn, "__tool_meta__")
            assert meta.category == "control"
            assert meta.namespace == SYSTEM_NAMESPACE


# ---------------------------------------------------------------------------
# New tools: web_search
# ---------------------------------------------------------------------------

class TestWebSearch:
    def test_web_search_metadata(self) -> None:
        from agent_framework.tools.builtin.web import web_search
        meta = getattr(web_search, "__tool_meta__")
        assert meta.category == "network"
        assert meta.namespace == SYSTEM_NAMESPACE
        assert "search" in meta.tags

    def test_web_search_returns_structure(self) -> None:
        """web_search should return dict with query and results fields."""
        from agent_framework.tools.builtin.web import web_search

        # We can't reliably test actual web search in unit tests,
        # but we can verify the function signature and basic error handling
        # by testing with a query that should return empty from a blocked domain
        result = web_search("test query", max_results=1)
        assert "query" in result
        assert "results" in result
        assert result["query"] == "test query"


# ---------------------------------------------------------------------------
# Shell module extraction
# ---------------------------------------------------------------------------

class TestShellModuleExtraction:
    def test_shell_manager_importable(self) -> None:
        from agent_framework.tools.shell.shell_manager import BashSession
        session = BashSession()
        assert session._proc is None

    def test_process_registry_importable(self) -> None:
        from agent_framework.tools.shell.process_registry import \
            ShellSessionManager
        sessions = ShellSessionManager.list_sessions()
        assert isinstance(sessions, list)

    def test_backward_compat_imports(self) -> None:
        """Old private names should still be importable from builtin/shell.py."""
        from agent_framework.tools.builtin.shell import (_ENV_WHITELIST,
                                                         _build_safe_env,
                                                         _check_banned,
                                                         _ShellSessionManager)
        assert callable(_build_safe_env)
        assert callable(_check_banned)
        assert isinstance(_ENV_WHITELIST, frozenset)

    def test_banned_commands(self) -> None:
        from agent_framework.tools.shell.shell_manager import check_banned
        assert check_banned("curl http://example.com") is not None
        assert check_banned("sudo rm -rf /") is not None
        assert check_banned("pip install malware") is not None
        assert check_banned("echo hello") is None
        assert check_banned("python -m pytest") is None

    def test_safe_env_no_secrets(self) -> None:
        import os

        from agent_framework.tools.shell.shell_manager import build_safe_env
        os.environ["SECRET_API_KEY"] = "should_not_leak"
        env = build_safe_env()
        assert "SECRET_API_KEY" not in env
        del os.environ["SECRET_API_KEY"]


# ---------------------------------------------------------------------------
# Parameter schemas
# ---------------------------------------------------------------------------

class TestParameterSchemas:
    def test_all_schemas_importable(self) -> None:
        from agent_framework.tools.schemas import (BashExecArgs,
                                                   BashOutputArgs,
                                                   CheckSpawnResultArgs,
                                                   ClearMemoriesArgs,
                                                   EditFileArgs,
                                                   ForgetMemoryArgs,
                                                   GlobFilesArgs,
                                                   GrepSearchArgs,
                                                   ListMemoriesArgs,
                                                   NotebookEditArgs,
                                                   ReadFileArgs,
                                                   SlashCommandArgs,
                                                   SpawnAgentArgs,
                                                   TaskCreateArgs, TaskGetArgs,
                                                   TaskUpdateArgs, ThinkArgs,
                                                   WebFetchArgs, WebSearchArgs,
                                                   WriteFileArgs)

        # All imported successfully
        assert ReadFileArgs is not None

    def test_schema_defaults(self) -> None:
        from agent_framework.tools.schemas.builtin_args import (BashExecArgs,
                                                                GrepSearchArgs,
                                                                WebSearchArgs)
        bash = BashExecArgs(command="echo hello")
        assert bash.timeout_seconds == 120
        assert bash.run_in_background is False

        search = WebSearchArgs(query="test")
        assert search.max_results == 5
        assert search.allowed_domains is None

        grep = GrepSearchArgs(pattern="test")
        assert grep.path == "."
        assert grep.case_insensitive is False

    def test_tool_category_constants(self) -> None:
        assert ToolCategory.FILESYSTEM == "filesystem"
        assert ToolCategory.SYSTEM == "system"
        assert ToolCategory.NETWORK == "network"
        assert ToolCategory.DELEGATION == "delegation"
        assert ToolCategory.CONTROL == "control"
        assert ToolCategory.MEMORY_ADMIN == "memory_admin"
        assert ToolCategory.REASONING == "reasoning"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestRegistration:
    def _has(self, catalog, name: str) -> bool:
        """Check if tool exists in catalog by bare or qualified name."""
        return (
            catalog.get(f"local::{name}") is not None
            or catalog.get(f"subagent::{name}") is not None
        )

    def test_register_all_builtins_default(self) -> None:
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog
        catalog = GlobalToolCatalog()
        count = register_all_builtins(catalog)
        assert count >= 18  # core tools (no shell)

        # Shell tools should NOT be registered by default
        assert not self._has(catalog, "bash_exec")

        # Removed tools should not be registered
        assert not self._has(catalog, "run_command")
        assert not self._has(catalog, "get_env")
        assert not self._has(catalog, "file_exists")
        assert not self._has(catalog, "notebook_edit")

        # Core tools should be registered
        assert self._has(catalog, "read_file")
        assert self._has(catalog, "web_fetch")
        assert self._has(catalog, "web_search")
        assert self._has(catalog, "spawn_agent")
        assert self._has(catalog, "exit_plan_mode")
        # slash_command commented out — integration-layer specific
        assert not self._has(catalog, "slash_command")

    def test_register_all_builtins_with_shell(self) -> None:
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog
        catalog = GlobalToolCatalog()
        count = register_all_builtins(catalog, shell_enabled=True)
        assert self._has(catalog, "bash_exec")
        assert self._has(catalog, "bash_output")
        assert self._has(catalog, "bash_stop")
        assert self._has(catalog, "task_stop")
        assert self._has(catalog, "kill_shell")

    def test_register_without_web_search(self) -> None:
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog
        catalog = GlobalToolCatalog()
        register_all_builtins(catalog, web_search_enabled=False)
        assert not self._has(catalog, "web_search")
        assert self._has(catalog, "web_fetch")

    def test_register_without_control_tools(self) -> None:
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog
        catalog = GlobalToolCatalog()
        register_all_builtins(catalog, control_tools_enabled=False)
        assert not self._has(catalog, "slash_command")
        assert not self._has(catalog, "exit_plan_mode")
        assert self._has(catalog, "task_create")  # task tools still registered

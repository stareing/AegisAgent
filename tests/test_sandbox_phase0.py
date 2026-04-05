"""Tests for Sandbox Phase 0 security hardening.

Covers:
- Environment variable whitelist filtering
- Shell tool enable/disable flag
- get_env confirmation requirement
"""

from __future__ import annotations

import os

import pytest


class TestBuildSafeEnv:
    """Verify _build_safe_env() filters environment variables correctly."""

    def test_includes_whitelisted_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whitelisted variables like PATH, HOME, PYTHONPATH are preserved."""
        from agent_framework.tools.builtin.shell import _build_safe_env

        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("HOME", "/home/test")
        monkeypatch.setenv("PYTHONPATH", "/lib/python")
        monkeypatch.setenv("VIRTUAL_ENV", "/venv")

        env = _build_safe_env()
        assert env["PATH"] == "/usr/bin"
        assert env["HOME"] == "/home/test"
        assert env["PYTHONPATH"] == "/lib/python"
        assert env["VIRTUAL_ENV"] == "/venv"

    def test_excludes_secret_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Secret variables like API keys and tokens must be excluded."""
        from agent_framework.tools.builtin.shell import _build_safe_env

        secret_vars = [
            "AWS_SECRET_ACCESS_KEY",
            "AWS_ACCESS_KEY_ID",
            "API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GITHUB_TOKEN",
            "DATABASE_URL",
            "SECRET_KEY",
            "STRIPE_SECRET_KEY",
        ]
        for var in secret_vars:
            monkeypatch.setenv(var, "super-secret-value")

        env = _build_safe_env()
        for var in secret_vars:
            assert var not in env, f"{var} should be excluded from safe env"

    def test_propagates_sandbox_roots(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENT_FS_SANDBOX_ROOTS is always propagated when present."""
        from agent_framework.tools.builtin.shell import _build_safe_env

        monkeypatch.setenv("AGENT_FS_SANDBOX_ROOTS", "/safe/dir")
        env = _build_safe_env()
        assert env["AGENT_FS_SANDBOX_ROOTS"] == "/safe/dir"

    def test_omits_sandbox_roots_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """AGENT_FS_SANDBOX_ROOTS is not injected when not in os.environ."""
        from agent_framework.tools.builtin.shell import _build_safe_env

        monkeypatch.delenv("AGENT_FS_SANDBOX_ROOTS", raising=False)
        env = _build_safe_env()
        assert "AGENT_FS_SANDBOX_ROOTS" not in env

    def test_only_whitelisted_keys_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Result contains only whitelisted keys plus AGENT_FS_SANDBOX_ROOTS."""
        from agent_framework.tools.builtin.shell import (_ENV_WHITELIST,
                                                         _build_safe_env)

        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("RANDOM_VAR", "should_not_appear")
        monkeypatch.delenv("AGENT_FS_SANDBOX_ROOTS", raising=False)

        env = _build_safe_env()
        allowed = _ENV_WHITELIST | {"AGENT_FS_SANDBOX_ROOTS"}
        for key in env:
            assert key in allowed, f"Unexpected key '{key}' in safe env"


class TestShellEnabledFlag:
    """Verify tools.shell_enabled controls shell tool registration."""

    def test_shell_disabled_by_default(self) -> None:
        """ToolConfig.shell_enabled defaults to False."""
        from agent_framework.infra.config import ToolConfig

        cfg = ToolConfig()
        assert cfg.shell_enabled is False

    def test_shell_disabled_excludes_shell_tools(self) -> None:
        """When shell_enabled=False, shell tools are not registered."""
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog, shell_enabled=False)

        tool_names = {e.meta.name for e in catalog.list_all()}
        shell_tools = {"bash_exec", "bash_output", "kill_shell", "run_command"}
        for name in shell_tools:
            assert name not in tool_names, (
                f"{name} should not be registered when shell_enabled=False"
            )

    def test_shell_enabled_includes_shell_tools(self) -> None:
        """When shell_enabled=True, shell tools are registered."""
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog, shell_enabled=True)

        tool_names = {e.meta.name for e in catalog.list_all()}
        # run_command/get_env removed — bash_exec covers both use cases
        shell_tools = {"bash_exec", "bash_output", "kill_shell"}
        for name in shell_tools:
            assert name in tool_names, (
                f"{name} should be registered when shell_enabled=True"
            )

    def test_get_env_via_bash(self) -> None:
        """get_env removed; env vars accessed via bash_exec('echo $VAR')."""
        from agent_framework.tools.builtin import register_all_builtins
        from agent_framework.tools.catalog import GlobalToolCatalog

        catalog = GlobalToolCatalog()
        register_all_builtins(catalog, shell_enabled=False)

        tool_names = {e.meta.name for e in catalog.list_all()}
        # get_env no longer registered as standalone tool
        assert "get_env" not in tool_names


class TestGetEnvConfirmation:
    """Verify get_env now requires confirmation."""

    def test_get_env_requires_confirm(self) -> None:
        """get_env tool must have require_confirm=True."""
        from agent_framework.tools.builtin.system import get_env

        meta = get_env.__tool_meta__
        assert meta.require_confirm is True, (
            "get_env should require confirmation to prevent silent env leaks"
        )

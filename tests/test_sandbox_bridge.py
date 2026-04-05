"""Tests for v5.0 sandbox bridge — risk-based shell execution routing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.tools.sandbox.protocol import SandboxResult
from agent_framework.tools.sandbox.risk_scorer import (
    RiskAssessment,
    RiskLevel,
    SandboxStrategy,
    score_command_risk,
)
from agent_framework.tools.sandbox.selector import SandboxSelector
from agent_framework.tools.shell.sandbox_bridge import SandboxBridge


# ===========================================================================
# Risk Scoring Integration
# ===========================================================================


class TestRiskScoring:

    def test_safe_command(self):
        result = score_command_risk("ls -la")
        assert result.level == RiskLevel.SAFE

    def test_low_risk_command(self):
        result = score_command_risk("mkdir -p build")
        assert result.level == RiskLevel.LOW

    def test_medium_risk_command(self):
        result = score_command_risk("curl https://example.com")
        assert result.level == RiskLevel.MEDIUM

    def test_high_risk_command(self):
        result = score_command_risk("chmod 644 /etc/config")
        assert result.level == RiskLevel.HIGH

    def test_critical_risk_command(self):
        result = score_command_risk("rm -rf /")
        assert result.level == RiskLevel.CRITICAL

    def test_empty_command_is_safe(self):
        result = score_command_risk("")
        assert result.level == RiskLevel.SAFE

    def test_unknown_command_defaults_low(self):
        result = score_command_risk("foobar_unknown_tool")
        assert result.level == RiskLevel.LOW

    def test_pipe_adds_risk_signal(self):
        result = score_command_risk("cat file.txt | grep pattern")
        assert result.score > 0
        assert any("+1: pipe chain" in r for r in result.reasons)


# ===========================================================================
# SandboxBridge
# ===========================================================================


class TestSandboxBridge:

    def _make_bridge(self, selector: SandboxSelector | None = None) -> SandboxBridge:
        if selector is None:
            selector = MagicMock(spec=SandboxSelector)
        return SandboxBridge(selector, min_risk_for_sandbox=RiskLevel.MEDIUM)

    @pytest.mark.asyncio
    async def test_low_risk_uses_session(self):
        """SAFE/LOW commands should delegate to native BashSession."""
        bridge = self._make_bridge()
        session = AsyncMock()
        session.execute.return_value = {"output": "ok", "exit_code": 0, "timed_out": False}

        result = await bridge.evaluate_and_execute(
            "ls -la", timeout_seconds=30, session=session,
        )

        session.execute.assert_called_once_with("ls -la", 30)
        assert result["exit_code"] == 0
        assert result["risk_level"] == "SAFE"
        assert result["sandbox_strategy"] == "native_session"

    @pytest.mark.asyncio
    async def test_medium_risk_uses_selector(self):
        """MEDIUM risk commands should route through SandboxSelector."""
        selector = AsyncMock(spec=SandboxSelector)
        selector.execute.return_value = (
            SandboxResult(exit_code=0, stdout="result", stderr=""),
            RiskAssessment(level=RiskLevel.MEDIUM, score=4, reasons=["network request"]),
        )
        bridge = SandboxBridge(selector, min_risk_for_sandbox=RiskLevel.MEDIUM)
        session = AsyncMock()

        result = await bridge.evaluate_and_execute(
            "curl https://example.com", timeout_seconds=60, session=session,
        )

        selector.execute.assert_called_once()
        session.execute.assert_not_called()
        assert result["exit_code"] == 0
        assert result["output"] == "result"
        assert result["sandbox_strategy"] == "sandbox_selector"

    @pytest.mark.asyncio
    async def test_no_session_uses_selector_for_all(self):
        """Without a session, even low-risk commands use SandboxSelector."""
        selector = AsyncMock(spec=SandboxSelector)
        selector.execute.return_value = (
            SandboxResult(exit_code=0, stdout="files", stderr=""),
            RiskAssessment(level=RiskLevel.SAFE, score=0),
        )
        bridge = SandboxBridge(selector, min_risk_for_sandbox=RiskLevel.MEDIUM)

        result = await bridge.evaluate_and_execute("ls", session=None)
        selector.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_stderr_appended_to_output(self):
        """Stderr should be appended to stdout in the result."""
        selector = AsyncMock(spec=SandboxSelector)
        selector.execute.return_value = (
            SandboxResult(exit_code=1, stdout="out", stderr="err"),
            RiskAssessment(level=RiskLevel.MEDIUM, score=4),
        )
        bridge = SandboxBridge(selector, min_risk_for_sandbox=RiskLevel.MEDIUM)

        result = await bridge.evaluate_and_execute("bad_cmd", session=None)
        assert "err" in result["output"]
        assert result["exit_code"] == 1

    def test_is_available_delegates(self):
        selector = MagicMock(spec=SandboxSelector)
        selector.is_available.return_value = True
        bridge = SandboxBridge(selector)
        assert bridge.is_available is True


# ===========================================================================
# Shell Tool Integration (set_sandbox_bridge)
# ===========================================================================


class TestShellToolBridgeWiring:

    def test_set_sandbox_bridge(self):
        from agent_framework.tools.builtin.shell import set_sandbox_bridge, _sandbox_bridge
        bridge = MagicMock()
        set_sandbox_bridge(bridge)
        from agent_framework.tools.builtin import shell
        assert shell._sandbox_bridge is bridge
        # Cleanup
        set_sandbox_bridge(None)

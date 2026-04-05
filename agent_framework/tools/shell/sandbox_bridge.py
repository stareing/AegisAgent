"""Bridge between shell tool execution and multi-strategy sandbox.

Connects the risk scorer + SandboxSelector (which were previously dead code)
into the bash_exec tool's execution path. When sandbox_auto_select is enabled,
commands are risk-scored and routed to the appropriate sandbox strategy.

Execution flow:
  bash_exec(command)
    → check_banned()           (fast blocklist — kept as-is)
    → SandboxBridge.evaluate()
      → score_command_risk()   (pattern-based risk level)
      → SandboxSelector.execute()
        → SAFE/LOW: native subprocess
        → MEDIUM: container sandbox
        → HIGH/CRITICAL: confirmation + strict container
"""

from __future__ import annotations

from typing import Any, Protocol

from agent_framework.infra.logger import get_logger
from agent_framework.tools.sandbox.risk_scorer import RiskLevel
from agent_framework.tools.sandbox.selector import SandboxSelector

logger = get_logger(__name__)


class ConfirmationHandler(Protocol):
    """Minimal confirmation protocol for sandbox bridge."""

    async def request_confirmation(
        self, action: str, context: dict[str, Any], message: str,
    ) -> bool: ...


class SandboxBridge:
    """Routes shell commands through risk-based sandbox selection.

    When enabled, replaces direct BashSession.execute() for foreground
    commands. Background commands still use BashSession.execute_background()
    directly (they run as independent subprocesses).
    """

    def __init__(
        self,
        selector: SandboxSelector,
        *,
        min_risk_for_sandbox: RiskLevel = RiskLevel.MEDIUM,
    ) -> None:
        self._selector = selector
        self._min_risk = min_risk_for_sandbox

    async def evaluate_and_execute(
        self,
        command: str,
        *,
        timeout_seconds: int = 120,
        session: Any = None,
    ) -> dict[str, Any]:
        """Score command risk and execute with appropriate sandbox.

        For SAFE/LOW risk: delegates to native BashSession if provided,
        otherwise uses SandboxSelector's native executor.

        For MEDIUM+ risk: routes through container sandbox.

        Returns dict with 'output', 'exit_code', 'timed_out', and
        optional 'risk_level', 'risk_score', 'sandbox_strategy' metadata.
        """
        from agent_framework.tools.sandbox.risk_scorer import score_command_risk

        assessment = score_command_risk(command)

        # Below minimum risk: use native BashSession for session persistence
        if assessment.level < self._min_risk and session is not None:
            result = await session.execute(command, timeout_seconds)
            result["risk_level"] = assessment.level.name
            result["risk_score"] = assessment.score
            result["sandbox_strategy"] = "native_session"
            return result

        # At or above risk threshold: route through SandboxSelector
        sandbox_result, final_assessment = await self._selector.execute(
            command,
            timeout=timeout_seconds,
        )

        logger.info(
            "sandbox_bridge.executed",
            risk_level=final_assessment.level.name,
            risk_score=final_assessment.score,
            command_preview=command[:80],
        )

        return {
            "output": sandbox_result.stdout + (
                f"\n{sandbox_result.stderr}" if sandbox_result.stderr else ""
            ),
            "exit_code": sandbox_result.exit_code,
            "timed_out": sandbox_result.timed_out,
            "risk_level": final_assessment.level.name,
            "risk_score": final_assessment.score,
            "sandbox_strategy": "sandbox_selector",
        }

    @property
    def is_available(self) -> bool:
        """Check if container sandbox runtime is available."""
        return self._selector.is_available()

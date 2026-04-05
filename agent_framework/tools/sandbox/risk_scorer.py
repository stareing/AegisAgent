"""Command risk scoring and multi-level sandbox auto-selection.

Inspired by Gemini CLI's multi-strategy sandbox approach:
- Assigns a RiskLevel to each shell command based on pattern matching
- Automatically selects the appropriate sandbox strategy based on risk

Risk levels map to sandbox strategies:
  SAFE       → no sandbox (pure read-only commands)
  LOW        → native process isolation (ulimits, tmpdir)
  MEDIUM     → container sandbox (Docker/Podman with restrictions)
  HIGH       → strict container sandbox (read-only root, no network)
  CRITICAL   → blocked entirely (requires explicit user approval)
"""

from __future__ import annotations

import re
import shlex
from enum import IntEnum
from typing import Any

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class RiskLevel(IntEnum):
    """Command risk classification — higher value = more dangerous."""

    SAFE = 0
    LOW = 1
    MEDIUM = 2
    HIGH = 3
    CRITICAL = 4


class RiskAssessment(BaseModel):
    """Result of command risk scoring."""

    model_config = {"frozen": True}

    level: RiskLevel = RiskLevel.MEDIUM
    score: int = 0
    reasons: list[str] = Field(default_factory=list)
    matched_patterns: list[str] = Field(default_factory=list)


class SandboxStrategy(BaseModel):
    """Resolved sandbox execution strategy."""

    model_config = {"frozen": True}

    name: str = "native"  # "none" | "native" | "container" | "strict_container"
    require_confirmation: bool = False
    container_read_only: bool = False
    container_network: str = "none"
    container_memory_limit: str = "512m"
    container_pids_limit: int = 256


# ── Pattern definitions ──────────────────────────────────────────────

# Patterns that indicate SAFE commands (read-only, no side effects)
_SAFE_PATTERNS: list[tuple[str, str]] = [
    (r"^(cat|head|tail|less|more)\s", "read-only file viewer"),
    (r"^(ls|dir|find|locate|which|whereis)\s", "directory listing"),
    (r"^(echo|printf)\s", "output command"),
    (r"^(wc|sort|uniq|diff|comm)\s", "text processing"),
    (r"^(date|uptime|hostname|whoami|id|uname)\b", "system info query"),
    (r"^(pwd|env|printenv)\b", "environment query"),
    (r"^(grep|rg|ag|ack)\s", "search command"),
    (r"^(file|stat|du|df)\s", "file info query"),
    (r"^git\s+(status|log|diff|show|branch|tag|describe|rev-parse)\b", "git read"),
    (r"^python\s+-c\s+['\"].*print", "python print expression"),
]

# Patterns that indicate LOW risk (limited write, confined effects)
_LOW_PATTERNS: list[tuple[str, str]] = [
    (r"^(mkdir|touch)\s", "create file/directory"),
    (r"^(cp|mv)\s", "copy/move file"),
    (r"^(pip|pip3)\s+install\b", "package install"),
    (r"^(npm|yarn|pnpm)\s+install\b", "package install"),
    (r"^git\s+(add|commit|stash|checkout|switch|merge|rebase)\b", "git write"),
    (r"^(python|python3|node)\s+\S+\.(py|js|ts)\b", "script execution"),
    (r"^pytest\b", "test runner"),
    (r"^(make|cargo|go)\s+(build|test|check|fmt)\b", "build/test command"),
]

# Patterns that indicate MEDIUM risk (broader system access)
_MEDIUM_PATTERNS: list[tuple[str, str]] = [
    (r"^(sed|awk)\s+.*-i\b", "in-place file modification"),
    (r"\bsudo\b", "elevated privileges"),
    (r"^git\s+push\b", "git push"),
    (r"^docker\s+(run|exec|build)\b", "container operation"),
    (r"^(curl|wget|fetch)\s", "network request"),
    (r"\b(pip|npm)\s+(publish|upload)\b", "package publish"),
]

# Patterns that indicate HIGH risk (destructive or system-wide effects)
_HIGH_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+(-rf?|--recursive)\b", "recursive delete"),
    (r"\brm\s+-[a-zA-Z]*f", "force delete"),
    (r"^(chmod|chown|chgrp)\s", "permission change"),
    (r"\b(mkfs|fdisk|dd)\b", "disk operation"),
    (r"\b(systemctl|service)\s+(stop|restart|disable)\b", "service management"),
    (r"\b(kill|killall|pkill)\s", "process termination"),
    (r"^git\s+(push\s+--force|reset\s+--hard|clean\s+-[a-zA-Z]*f)\b", "destructive git"),
    (r"\b>\s*/etc/", "write to system config"),
    (r"\b(iptables|ufw|firewall-cmd)\b", "firewall modification"),
]

# Patterns that indicate CRITICAL risk (never auto-execute)
_CRITICAL_PATTERNS: list[tuple[str, str]] = [
    (r"\brm\s+-rf\s+/", "root filesystem delete"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "system shutdown"),
    (r"\b(mkfs|wipefs)\b", "filesystem format"),
    (r":(){.*};:", "fork bomb"),
    (r"\b(passwd|useradd|userdel|usermod)\b", "user management"),
    (r"\bchmod\s+777\b", "world-writable permission"),
    (r"\bcurl\s.*\|\s*(sh|bash)\b", "pipe to shell"),
    (r"\beval\s.*\$\(", "eval with command substitution"),
]

# Additional risk signals (additive score)
_RISK_SIGNALS: list[tuple[str, int, str]] = [
    (r"\|", 1, "pipe chain"),
    (r"&&", 1, "command chain"),
    (r"\$\(", 2, "command substitution"),
    (r"`[^`]+`", 2, "backtick substitution"),
    (r">\s*\S+", 1, "output redirection"),
    (r"2>&1", 1, "stderr redirect"),
    (r"\bsudo\b", 3, "privilege escalation"),
    (r"\b(curl|wget).*\|\s*(bash|sh|python)", 5, "remote code execution"),
    (r"/dev/(sd|nvme|loop)", 3, "raw device access"),
    (r"\benv\b.*=.*\bsh\b", 3, "environment injection"),
]


def score_command_risk(command: str) -> RiskAssessment:
    """Score the risk level of a shell command.

    Returns a RiskAssessment with the highest matching risk level,
    cumulative score, and matched pattern descriptions.
    """
    command_stripped = command.strip()
    if not command_stripped:
        return RiskAssessment(level=RiskLevel.SAFE, score=0)

    max_level = RiskLevel.SAFE
    total_score = 0
    reasons: list[str] = []
    matched: list[str] = []

    # Check pattern tiers from highest to lowest
    for patterns, level in [
        (_CRITICAL_PATTERNS, RiskLevel.CRITICAL),
        (_HIGH_PATTERNS, RiskLevel.HIGH),
        (_MEDIUM_PATTERNS, RiskLevel.MEDIUM),
        (_LOW_PATTERNS, RiskLevel.LOW),
        (_SAFE_PATTERNS, RiskLevel.SAFE),
    ]:
        for pattern, description in patterns:
            if re.search(pattern, command_stripped):
                if level > max_level:
                    max_level = level
                total_score += level.value * 2
                reasons.append(f"{level.name}: {description}")
                matched.append(pattern)

    # Additive risk signals
    for pattern, score_add, description in _RISK_SIGNALS:
        if re.search(pattern, command_stripped):
            total_score += score_add
            reasons.append(f"+{score_add}: {description}")
            matched.append(pattern)

    # If no patterns matched, default to MEDIUM (unknown command)
    if max_level == RiskLevel.SAFE and not matched:
        max_level = RiskLevel.LOW
        total_score = 2
        reasons.append("LOW: unrecognized command (default)")

    return RiskAssessment(
        level=max_level,
        score=total_score,
        reasons=reasons,
        matched_patterns=matched,
    )


# ── Risk → Sandbox strategy mapping ──────────────────────────────────

# Default strategy per risk level
_RISK_TO_STRATEGY: dict[RiskLevel, SandboxStrategy] = {
    RiskLevel.SAFE: SandboxStrategy(
        name="none",
        require_confirmation=False,
    ),
    RiskLevel.LOW: SandboxStrategy(
        name="native",
        require_confirmation=False,
    ),
    RiskLevel.MEDIUM: SandboxStrategy(
        name="container",
        require_confirmation=False,
        container_network="none",
        container_memory_limit="512m",
    ),
    RiskLevel.HIGH: SandboxStrategy(
        name="strict_container",
        require_confirmation=True,
        container_read_only=True,
        container_network="none",
        container_memory_limit="256m",
        container_pids_limit=128,
    ),
    RiskLevel.CRITICAL: SandboxStrategy(
        name="strict_container",
        require_confirmation=True,
        container_read_only=True,
        container_network="none",
        container_memory_limit="128m",
        container_pids_limit=64,
    ),
}


def select_sandbox_strategy(
    command: str,
    *,
    available_runtimes: list[str] | None = None,
    force_sandbox: str | None = None,
) -> tuple[RiskAssessment, SandboxStrategy]:
    """Score a command and select the appropriate sandbox strategy.

    Args:
        command: Shell command to assess.
        available_runtimes: List of available sandbox runtimes
            (e.g. ["docker", "podman"]). If none available, falls back
            to native isolation for container-requiring strategies.
        force_sandbox: Override strategy name (e.g. "container").

    Returns:
        Tuple of (risk_assessment, sandbox_strategy).
    """
    assessment = score_command_risk(command)

    if force_sandbox:
        # User-forced strategy — still return the assessment for logging
        strategy = SandboxStrategy(name=force_sandbox)
        return assessment, strategy

    strategy = _RISK_TO_STRATEGY.get(
        assessment.level,
        _RISK_TO_STRATEGY[RiskLevel.MEDIUM],
    )

    # Fallback: if container strategy selected but no runtime available,
    # downgrade to native with confirmation
    if strategy.name in ("container", "strict_container"):
        runtimes = available_runtimes or []
        if not runtimes:
            logger.warning(
                "sandbox.no_container_runtime",
                risk_level=assessment.level.name,
                fallback="native",
            )
            strategy = SandboxStrategy(
                name="native",
                require_confirmation=True,
            )

    logger.debug(
        "sandbox.strategy_selected",
        command_preview=command[:100],
        risk_level=assessment.level.name,
        risk_score=assessment.score,
        strategy=strategy.name,
        reasons=assessment.reasons[:3],
    )

    return assessment, strategy

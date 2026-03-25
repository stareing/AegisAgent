"""Declarative policy engine — TOML-based tool approval rules.

Inspired by Gemini CLI's policy engine with wildcard matching,
pattern-based shell command validation, and approval memory.

Policy rules are loaded from TOML files (or config dicts) and evaluated
at runtime to determine whether a tool call should be ALLOWED, DENIED,
or require user confirmation (ASK).

Rule format (TOML):
```toml
[[rules]]
tool = "bash_exec"              # exact name, or "mcp_*_*" for wildcards
approval = "ASK"                # ALLOW | DENY | ASK
modes = ["DEFAULT"]             # optional: only apply in these ApprovalModes
command_prefix = "git push"     # optional: match shell command prefix
args_pattern = ".*--force.*"    # optional: regex against serialized args

[[rules]]
tool = "mcp_*_*"                # wildcard: all MCP tools
approval = "ASK"

[[rules]]
tool = "*"                      # catch-all fallback
approval = "ALLOW"
```

Evaluation order:
1. Most specific rule wins (exact name > prefix wildcard > catch-all)
2. Within same specificity, first match wins
3. If no rule matches, default is ALLOW
"""

from __future__ import annotations

import fnmatch
import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class PolicyApproval(str, Enum):
    """Action to take when a policy rule matches."""

    ALLOW = "ALLOW"
    DENY = "DENY"
    ASK = "ASK"


class PolicyRule(BaseModel):
    """A single declarative policy rule."""

    tool: str = "*"
    approval: PolicyApproval = PolicyApproval.ALLOW
    modes: list[str] | None = None
    mcp_server: str | None = None
    command_prefix: str | None = None
    args_pattern: str | None = None
    subagent: str | None = None
    description: str = ""

    @property
    def specificity(self) -> int:
        """Higher = more specific rule. Used for priority ordering."""
        score = 0
        if self.tool != "*":
            score += 10
        if "*" not in self.tool:
            score += 5
        if self.command_prefix:
            score += 3
        if self.args_pattern:
            score += 2
        if self.modes:
            score += 1
        if self.mcp_server:
            score += 2
        return score


class PolicyDecision(BaseModel):
    """Result of policy evaluation."""

    model_config = {"frozen": True}

    approval: PolicyApproval = PolicyApproval.ALLOW
    matched_rule: PolicyRule | None = None
    reason: str = ""


class ApprovalMemory:
    """Remembers user approval decisions within a session.

    When a user approves/denies a tool in ASK mode, the decision is
    cached so the same tool+args pattern doesn't prompt again.
    """

    def __init__(self) -> None:
        self._decisions: dict[str, PolicyApproval] = {}

    def remember(self, key: str, decision: PolicyApproval) -> None:
        """Store an approval decision."""
        self._decisions[key] = decision

    def recall(self, key: str) -> PolicyApproval | None:
        """Recall a previous decision, or None if not seen."""
        return self._decisions.get(key)

    def make_key(self, tool_name: str, arguments: dict[str, Any] | None = None) -> str:
        """Build a cache key from tool name and optional arguments."""
        if arguments:
            # Include sorted args for deterministic key
            args_str = json.dumps(arguments, sort_keys=True, default=str)
            return f"{tool_name}::{args_str[:200]}"
        return tool_name

    def clear(self) -> None:
        """Clear all cached decisions."""
        self._decisions.clear()

    @property
    def decision_count(self) -> int:
        return len(self._decisions)


class DeclarativePolicyEngine:
    """Evaluates tool calls against declarative TOML policy rules.

    Thread-safe for reads. Rules are loaded at init and immutable.
    ApprovalMemory is session-scoped and mutable.
    """

    def __init__(
        self,
        rules: list[PolicyRule] | None = None,
    ) -> None:
        self._rules: list[PolicyRule] = sorted(
            rules or [],
            key=lambda r: r.specificity,
            reverse=True,  # most specific first
        )
        self._memory = ApprovalMemory()

    @classmethod
    def from_toml(cls, path: str | Path) -> DeclarativePolicyEngine:
        """Load policy rules from a TOML file."""
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        path = Path(path)
        if not path.exists():
            logger.warning("policy_engine.toml_not_found", path=str(path))
            return cls(rules=[])

        with open(path, "rb") as f:
            data = tomllib.load(f)

        raw_rules = data.get("rules", [])
        rules = [PolicyRule(**r) for r in raw_rules]
        logger.info("policy_engine.loaded", path=str(path), rule_count=len(rules))
        return cls(rules=rules)

    @classmethod
    def from_dicts(cls, rules: list[dict[str, Any]]) -> DeclarativePolicyEngine:
        """Create from a list of rule dicts (e.g. from JSON config)."""
        return cls(rules=[PolicyRule(**r) for r in rules])

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        current_mode: str | None = None,
    ) -> PolicyDecision:
        """Evaluate a tool call against policy rules.

        Args:
            tool_name: Fully qualified tool name (e.g. "mcp::server::tool").
            arguments: Tool call arguments.
            current_mode: Current ApprovalMode value string.

        Returns:
            PolicyDecision with the matched rule and approval action.
        """
        # Check approval memory first
        memory_key = self._memory.make_key(tool_name, arguments)
        cached = self._memory.recall(memory_key)
        if cached is not None:
            return PolicyDecision(
                approval=cached,
                reason=f"Cached decision from prior approval (key={memory_key[:50]})",
            )

        # Evaluate rules in specificity order
        for rule in self._rules:
            if self._matches(rule, tool_name, arguments, current_mode):
                logger.debug(
                    "policy_engine.matched",
                    tool=tool_name,
                    rule_tool=rule.tool,
                    approval=rule.approval.value,
                )
                return PolicyDecision(
                    approval=rule.approval,
                    matched_rule=rule,
                    reason=rule.description or f"Matched rule: {rule.tool}",
                )

        # Default: ALLOW (no matching rule)
        return PolicyDecision(
            approval=PolicyApproval.ALLOW,
            reason="No matching policy rule — default ALLOW",
        )

    def record_decision(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        decision: PolicyApproval,
    ) -> None:
        """Record a user's approval decision for future lookups."""
        key = self._memory.make_key(tool_name, arguments)
        self._memory.remember(key, decision)

    def reset_memory(self) -> None:
        """Clear approval memory (e.g. at session start)."""
        self._memory.clear()

    @property
    def rule_count(self) -> int:
        return len(self._rules)

    @property
    def memory(self) -> ApprovalMemory:
        return self._memory

    def _matches(
        self,
        rule: PolicyRule,
        tool_name: str,
        arguments: dict[str, Any] | None,
        current_mode: str | None,
    ) -> bool:
        """Check if a rule matches the given tool call context."""
        # Tool name matching (supports fnmatch wildcards)
        if rule.tool != "*":
            if not fnmatch.fnmatch(tool_name, rule.tool):
                return False

        # Mode filtering
        if rule.modes:
            if current_mode and current_mode not in rule.modes:
                return False

        # MCP server filtering
        if rule.mcp_server:
            # Tool name format: mcp::<server>::<tool>
            parts = tool_name.split("::")
            if len(parts) < 2 or parts[1] != rule.mcp_server:
                return False

        # Command prefix matching (for shell tools)
        if rule.command_prefix and arguments:
            command = arguments.get("command", "")
            if isinstance(command, str) and not command.startswith(rule.command_prefix):
                return False

        # Args pattern matching (regex against serialized args)
        if rule.args_pattern and arguments:
            args_str = json.dumps(arguments, default=str)
            if not re.search(rule.args_pattern, args_str):
                return False

        return True

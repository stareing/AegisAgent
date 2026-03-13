"""Skill body preprocessing — argument substitution and shell directives."""

from __future__ import annotations

import re
import subprocess

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# Pattern: !`command` (shell directive)
_SHELL_DIRECTIVE_RE = re.compile(r"!`([^`]+)`")


def substitute_arguments(body: str, raw_args: str) -> str:
    """Replace $ARGUMENTS, $0, $1, ... in skill body.

    $ARGUMENTS — full argument string
    $0, $1, $2 — positional (whitespace-split)
    If $ARGUMENTS not found in body, append as "ARGUMENTS: <value>"
    """
    if not raw_args:
        body = body.replace("$ARGUMENTS", "")
        # Clean positional placeholders
        body = re.sub(r"\$\d+", "", body)
        return body

    parts = raw_args.split()

    has_placeholder = "$ARGUMENTS" in body or re.search(r"\$\d+", body)

    # Replace $ARGUMENTS first
    body = body.replace("$ARGUMENTS", raw_args)

    # Replace positional $0, $1, $2, ...
    for i, part in enumerate(parts):
        body = body.replace(f"${i}", part)

    # Clean remaining unreplaced positional
    body = re.sub(r"\$\d+", "", body)

    # If no placeholder was found, append arguments
    if not has_placeholder:
        body = f"{body}\n\nARGUMENTS: {raw_args}"

    return body


def execute_shell_directives(body: str, cwd: str | None = None) -> str:
    """Execute !`command` directives and replace with stdout.

    Only runs for project-local skills. Shell commands execute at
    invocation time (preprocessing), not during LLM execution.
    """

    def _run_directive(match: re.Match) -> str:
        cmd = match.group(1).strip()
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
                cwd=cwd,
            )
            output = result.stdout.strip()
            if result.returncode != 0 and result.stderr:
                logger.warning(
                    "skill.shell_directive_error",
                    command=cmd,
                    stderr=result.stderr[:200],
                    returncode=result.returncode,
                )
            return output if output else f"[command returned empty: {cmd}]"
        except subprocess.TimeoutExpired:
            logger.error("skill.shell_directive_timeout", command=cmd)
            return f"[command timed out: {cmd}]"
        except Exception as e:
            logger.error("skill.shell_directive_failed", command=cmd, error=str(e))
            return f"[command failed: {cmd}: {e}]"

    return _SHELL_DIRECTIVE_RE.sub(_run_directive, body)


def preprocess_skill(
    body: str,
    raw_args: str = "",
    cwd: str | None = None,
    enable_shell: bool = True,
) -> str:
    """Full preprocessing pipeline: arguments → shell directives."""
    body = substitute_arguments(body, raw_args)
    if enable_shell and _SHELL_DIRECTIVE_RE.search(body):
        body = execute_shell_directives(body, cwd=cwd)
    return body

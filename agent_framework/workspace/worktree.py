"""Git worktree isolation — per-agent/session isolated branches.

Creates a git worktree for each agent run so file operations happen
in an isolated branch. On exit, the worktree can be kept (for later
merge) or removed.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent_framework.infra.logger import get_logger
from agent_framework.models.tool import ErrorCode

logger = get_logger(__name__)

# Default worktree storage under project root
_WORKTREE_DIR = ".agent_framework/worktrees"


@dataclass(frozen=True)
class WorktreeSession:
    """Active worktree session state."""

    worktree_path: str
    branch_name: str
    original_cwd: str
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class WorktreeError(Exception):
    """Raised when a git worktree operation fails."""

    def __init__(self, message: str, error_code: str = ErrorCode.WORKTREE_FAILED):
        super().__init__(message)
        self.error_code = error_code


def _run_git(args: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a git command and return the result."""
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=30,
    )


def _is_git_repo(path: str) -> bool:
    """Check if the given path is inside a git repository."""
    result = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=path)
    return result.returncode == 0


def _git_root(path: str) -> str | None:
    """Get the git repository root."""
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


class WorktreeManager:
    """Manages git worktree creation and cleanup for agent isolation.

    Each agent run can optionally execute in its own worktree,
    providing branch-level isolation for file operations.
    """

    def __init__(self, worktree_base_dir: str | None = None) -> None:
        self._base_dir = worktree_base_dir
        self._active_sessions: dict[str, WorktreeSession] = {}

    def _resolve_base_dir(self, cwd: str) -> Path:
        """Resolve the base directory for worktrees."""
        if self._base_dir:
            return Path(self._base_dir)
        root = _git_root(cwd)
        if root:
            return Path(root) / _WORKTREE_DIR
        return Path(cwd) / _WORKTREE_DIR

    def enter_worktree(
        self,
        run_id: str,
        branch_prefix: str = "agent",
        cwd: str | None = None,
    ) -> WorktreeSession:
        """Create a new git worktree and return the session.

        Args:
            run_id: Unique run identifier (used in branch name).
            branch_prefix: Prefix for the worktree branch name.
            cwd: Current working directory (defaults to os.getcwd()).

        Returns:
            WorktreeSession with worktree path and branch info.

        Raises:
            WorktreeError: If git worktree creation fails.
        """
        import os
        original_cwd = cwd or os.getcwd()

        if not _is_git_repo(original_cwd):
            raise WorktreeError(
                f"Not a git repository: {original_cwd}",
            )

        # Generate unique branch and path
        short_id = run_id[:12].replace("-", "")
        branch_name = f"{branch_prefix}/{short_id}"
        base_dir = self._resolve_base_dir(original_cwd)
        base_dir.mkdir(parents=True, exist_ok=True)
        worktree_path = base_dir / short_id

        # Create worktree with new branch
        result = _run_git(
            ["worktree", "add", "-b", branch_name, str(worktree_path)],
            cwd=original_cwd,
        )
        if result.returncode != 0:
            raise WorktreeError(
                f"Failed to create worktree: {result.stderr.strip()}",
            )

        session = WorktreeSession(
            worktree_path=str(worktree_path),
            branch_name=branch_name,
            original_cwd=original_cwd,
        )
        self._active_sessions[run_id] = session

        logger.info(
            "worktree.entered",
            run_id=run_id,
            worktree_path=str(worktree_path),
            branch=branch_name,
        )
        return session

    def exit_worktree(
        self,
        run_id: str,
        keep: bool = True,
        discard_changes: bool = False,
    ) -> None:
        """Exit and optionally clean up a worktree.

        Args:
            run_id: The run that owns the worktree.
            keep: If True, keep the worktree and branch for later merge.
            discard_changes: If True, discard uncommitted changes before removal.
        """
        session = self._active_sessions.pop(run_id, None)
        if session is None:
            logger.warning("worktree.exit_no_session", run_id=run_id)
            return

        worktree_path = Path(session.worktree_path)

        if not keep:
            if discard_changes:
                _run_git(["checkout", "--", "."], cwd=str(worktree_path))
                _run_git(["clean", "-fd"], cwd=str(worktree_path))

            # Remove worktree
            result = _run_git(
                ["worktree", "remove", str(worktree_path), "--force"],
                cwd=session.original_cwd,
            )
            if result.returncode != 0:
                logger.warning(
                    "worktree.remove_failed",
                    run_id=run_id,
                    error=result.stderr.strip(),
                )
                # Fallback: force remove directory
                if worktree_path.exists():
                    shutil.rmtree(str(worktree_path), ignore_errors=True)

            # Delete the branch
            _run_git(
                ["branch", "-D", session.branch_name],
                cwd=session.original_cwd,
            )

            logger.info(
                "worktree.removed",
                run_id=run_id,
                worktree_path=str(worktree_path),
            )
        else:
            logger.info(
                "worktree.kept",
                run_id=run_id,
                worktree_path=str(worktree_path),
                branch=session.branch_name,
            )

    def get_session(self, run_id: str) -> WorktreeSession | None:
        """Get the active worktree session for a run."""
        return self._active_sessions.get(run_id)

    def has_uncommitted_changes(self, run_id: str) -> bool:
        """Check if the worktree has uncommitted changes."""
        session = self._active_sessions.get(run_id)
        if not session:
            return False
        result = _run_git(["status", "--porcelain"], cwd=session.worktree_path)
        return bool(result.stdout.strip())

    def cleanup_all(self, discard_changes: bool = True) -> None:
        """Clean up all active worktrees. Called on framework shutdown."""
        for run_id in list(self._active_sessions.keys()):
            try:
                self.exit_worktree(
                    run_id, keep=False, discard_changes=discard_changes,
                )
            except Exception as e:
                logger.warning(
                    "worktree.cleanup_error",
                    run_id=run_id,
                    error=str(e),
                )

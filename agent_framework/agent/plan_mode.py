"""Plan mode controller — manages plan mode lifecycle.

Responsibilities:
- Enter/exit plan mode with permission mode transitions
- Plan file creation and validation
- Slug generation for plan file naming
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path

from agent_framework.infra.logger import get_logger
from agent_framework.models.agent import ApprovalMode, PlanModeState

logger = get_logger(__name__)

# Default directory for plan files
_DEFAULT_PLAN_DIR = ".agent_framework/plans"


def _generate_plan_slug(task: str) -> str:
    """Generate a URL-safe slug from the task description for plan file naming."""
    # Use first 40 chars of task + hash for uniqueness
    prefix = "".join(c if c.isalnum() or c in "-_" else "-" for c in task[:40]).strip("-")
    if not prefix:
        prefix = "plan"
    suffix = hashlib.md5(f"{task}{time.time()}".encode()).hexdigest()[:8]
    return f"{prefix}-{suffix}"


class PlanModeController:
    """Manages plan mode transitions and plan file lifecycle.

    Plan mode is a restricted execution mode where only read-only tools
    are available. The agent explores the codebase and writes a plan
    file before transitioning back to normal execution.
    """

    def __init__(self, plan_dir: str | None = None) -> None:
        self._plan_dir = Path(plan_dir) if plan_dir else Path.cwd() / _DEFAULT_PLAN_DIR

    def enter_plan(
        self,
        current_approval_mode: ApprovalMode,
        task: str = "",
    ) -> PlanModeState:
        """Enter plan mode: save current mode, generate plan file path.

        Returns the new PlanModeState to be stored in AgentState.
        """
        slug = _generate_plan_slug(task)
        self._plan_dir.mkdir(parents=True, exist_ok=True)
        plan_file = self._plan_dir / f"{slug}.md"

        state = PlanModeState(
            active=True,
            pre_plan_approval_mode=current_approval_mode,
            plan_file_path=str(plan_file),
            plan_slug=slug,
        )

        logger.info(
            "plan_mode.entered",
            plan_file=str(plan_file),
            pre_mode=current_approval_mode.value,
        )
        return state

    def exit_plan(self, plan_state: PlanModeState) -> ApprovalMode:
        """Exit plan mode: validate plan file exists, return previous mode.

        Returns the ApprovalMode to restore.
        Raises ValueError if plan file does not exist.
        """
        if not plan_state.active:
            raise ValueError("Plan mode is not active")

        if plan_state.plan_file_path:
            plan_path = Path(plan_state.plan_file_path)
            if not plan_path.is_file():
                logger.warning(
                    "plan_mode.exit_no_file",
                    plan_file=plan_state.plan_file_path,
                )
                # Allow exit even without file — the plan may have been
                # submitted via exit_plan_mode tool instead

        restored_mode = plan_state.pre_plan_approval_mode
        logger.info(
            "plan_mode.exited",
            restored_mode=restored_mode.value,
            plan_file=plan_state.plan_file_path,
        )
        return restored_mode

    def write_plan(self, plan_state: PlanModeState, content: str) -> str:
        """Write or append content to the plan file.

        Returns the plan file path.
        """
        if not plan_state.active:
            raise ValueError("Plan mode is not active")

        if not plan_state.plan_file_path:
            raise ValueError("No plan file path configured")

        plan_path = Path(plan_state.plan_file_path)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(content, encoding="utf-8")

        logger.info(
            "plan_mode.plan_written",
            plan_file=str(plan_path),
            content_length=len(content),
        )
        return str(plan_path)

    def read_plan(self, plan_state: PlanModeState) -> str | None:
        """Read the current plan file content, if it exists."""
        if not plan_state.plan_file_path:
            return None
        plan_path = Path(plan_state.plan_file_path)
        if not plan_path.is_file():
            return None
        return plan_path.read_text(encoding="utf-8")

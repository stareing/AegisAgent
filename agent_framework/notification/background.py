"""BackgroundNotifier — auto-notification for completed background tasks.

Background tasks run as independent subprocesses (not through BashSession's
locked execute()). The BackgroundNotifier tracks pending task_ids and drains
completed results before each LLM call.

Lifecycle:
- Instance-level on RunCoordinator (survives across runs)
- Tasks that outlive one run are drained at the start of the next
- Never cleared on run end — only individual completed tasks are removed

Architecture:
    bash_exec(run_in_background=True)
        → BashSession.execute_background() spawns independent subprocess
        → returns task_id
    coordinator._register_background_tasks()
        → notifier.register(task_id)
    coordinator._drain_background_notifications() [before each LLM call]
        → notifier.drain() polls BashSession for completed tasks
        → injects <background-results> user message into SessionState
"""

from __future__ import annotations


class BackgroundNotification:
    """A single notification from a completed background task."""

    __slots__ = ("task_id", "command", "output", "exit_code", "timed_out")

    def __init__(
        self,
        task_id: str,
        command: str = "",
        output: str = "",
        exit_code: int = 0,
        timed_out: bool = False,
    ) -> None:
        self.task_id = task_id
        self.command = command
        self.output = output
        self.exit_code = exit_code
        self.timed_out = timed_out

    def format_message(self) -> str:
        """Format as a concise notification string."""
        status = "timed out" if self.timed_out else (
            "success" if self.exit_code == 0 else f"exit={self.exit_code}"
        )
        preview = self.output[:1000]
        if len(self.output) > 1000:
            preview += f"\n... [{len(self.output) - 1000} chars truncated]"
        return f"[bg:{self.task_id}] ({status}) {preview}"


class BackgroundNotifier:
    """Drains completed background tasks from BashSession.

    Instance-level on RunCoordinator — survives across runs so that
    tasks spawned in run N can be drained at the start of run N+1.
    """

    def __init__(self) -> None:
        self._pending_task_ids: dict[str, str] = {}  # task_id → command

    def register(self, task_id: str, command: str = "") -> None:
        """Track a newly spawned background task."""
        self._pending_task_ids[task_id] = command

    def drain(self) -> list[BackgroundNotification]:
        """Check all pending tasks and return notifications for completed ones.

        Non-blocking: only returns results that are already available.
        Tasks still running remain in the pending set for the next drain.
        """
        if not self._pending_task_ids:
            return []

        try:
            from agent_framework.tools.shell.process_registry import \
                ShellSessionManager
        except ImportError:
            return []

        notifications: list[BackgroundNotification] = []
        completed_ids: list[str] = []

        for task_id, command in self._pending_task_ids.items():
            try:
                session = ShellSessionManager.get("default")
                result = session.get_background_result(task_id)
            except (ValueError, RuntimeError):
                notifications.append(BackgroundNotification(
                    task_id=task_id,
                    command=command,
                    output="Error: task not found or shell session terminated",
                    exit_code=-1,
                ))
                completed_ids.append(task_id)
                continue

            if result is not None:
                notifications.append(BackgroundNotification(
                    task_id=task_id,
                    command=command,
                    output=str(result.get("output", "")),
                    exit_code=int(result.get("exit_code", 0)),
                    timed_out=bool(result.get("timed_out", False)),
                ))
                completed_ids.append(task_id)

        for tid in completed_ids:
            self._pending_task_ids.pop(tid, None)

        return notifications

    @property
    def pending_count(self) -> int:
        return len(self._pending_task_ids)

    @property
    def has_pending(self) -> bool:
        return bool(self._pending_task_ids)

    def clear(self) -> None:
        """Drop all tracking. Only for shutdown, NOT for run boundaries."""
        self._pending_task_ids.clear()

    @staticmethod
    def format_notifications(notifications: list[BackgroundNotification]) -> str:
        """Format a batch of notifications as an XML block for context injection."""
        if not notifications:
            return ""
        lines = ["<background-results>"]
        for n in notifications:
            lines.append(n.format_message())
        lines.append("</background-results>")
        return "\n".join(lines)

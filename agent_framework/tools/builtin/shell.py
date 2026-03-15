"""Built-in persistent shell tools.

Provides a persistent bash session that maintains working directory and
environment across commands, with support for background execution.
"""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from typing import Any

from agent_framework.tools.decorator import tool

_DEFAULT_TIMEOUT = 120  # seconds
_MAX_OUTPUT_CHARS = 100_000

# Commands banned for security — network access, browsers, privilege escalation, etc.
_BANNED_COMMANDS = frozenset({
    "curl", "wget", "nc", "telnet", "lynx", "w3m", "links",
    "chrome", "firefox", "safari", "aria2c", "axel",
    "sudo", "su", "chmod", "chown", "mount", "umount",
    "iptables", "systemctl", "apt", "yum",
})

# Two-token banned commands (e.g. "pip install", "npm install").
_BANNED_TWO_TOKEN_COMMANDS = frozenset({
    ("pip", "install"),
    ("npm", "install"),
})


def _check_banned(command: str) -> str | None:
    """Return error message if command starts with a banned prefix."""
    tokens = command.strip().split()
    if not tokens:
        return None
    first_token = tokens[0]
    if first_token in _BANNED_COMMANDS:
        return f"Command '{first_token}' is blocked for security. Use web_fetch for HTTP requests."
    if len(tokens) >= 2:
        pair = (tokens[0], tokens[1])
        if pair in _BANNED_TWO_TOKEN_COMMANDS:
            label = f"{pair[0]} {pair[1]}"
            return f"Command '{label}' is blocked for security."
    return None


class _BashSession:
    """Manages a persistent bash subprocess.

    The session survives across tool calls so that working directory,
    environment variables, and shell state are preserved.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        self._background_tasks: dict[str, asyncio.Task[dict]] = {}
        self._background_results: dict[str, dict] = {}

    async def _ensure_started(self) -> asyncio.subprocess.Process:
        if self._proc is None or self._proc.returncode is not None:
            self._proc = await asyncio.create_subprocess_exec(
                "bash", "--norc", "--noprofile",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env={**os.environ, "TERM": "dumb", "PS1": "", "PS2": ""},
                start_new_session=True,
            )
        return self._proc

    async def _probe_health(self, proc: asyncio.subprocess.Process) -> bool:
        """Send a health-check echo and verify the session responds."""
        if proc.stdin is None or proc.stdout is None:
            return False
        probe_nonce = uuid.uuid4().hex
        try:
            probe_cmd = f"printf '\\n{probe_nonce}:0\\n'\n"
            proc.stdin.write(probe_cmd.encode())
            await proc.stdin.drain()
            line_bytes = await asyncio.wait_for(
                proc.stdout.readline(),
                timeout=5,
            )
            if not line_bytes:
                return False
            return probe_nonce in line_bytes.decode(errors="replace")
        except (asyncio.TimeoutError, OSError):
            return False

    async def _rebuild(self) -> None:
        """Kill the current process and start a fresh one."""
        if self._proc is not None:
            try:
                if self._proc.pid:
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
            self._proc = None
        await self._ensure_started()

    async def execute(
        self,
        command: str,
        timeout_seconds: int = _DEFAULT_TIMEOUT,
    ) -> dict:
        """Execute a command in the persistent bash session.

        Returns dict with 'output', 'exit_code', and 'timed_out' fields.
        """
        async with self._lock:
            proc = await self._ensure_started()
            assert proc.stdin is not None
            assert proc.stdout is not None

            # Per-execution random nonce to avoid sentinel collisions.
            nonce = uuid.uuid4().hex

            # Write command + sentinel echo so we know when output ends.
            wrapped = (
                f"{command}\n"
                f"__exit_code__=$?\n"
                f"printf '\\n{nonce}:%s\\n' \"$__exit_code__\"\n"
            )
            proc.stdin.write(wrapped.encode())
            await proc.stdin.drain()

            # Read output until sentinel.
            output_lines: list[str] = []
            timed_out = False
            try:
                while True:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=timeout_seconds,
                    )
                    if not line_bytes:
                        # Process died
                        break
                    line = line_bytes.decode(errors="replace")
                    if line.startswith(nonce):
                        # Extract exit code from sentinel line
                        parts = line.strip().split(":")
                        exit_code = int(parts[1]) if len(parts) > 1 else -1
                        return {
                            "output": _truncate("".join(output_lines)),
                            "exit_code": exit_code,
                            "timed_out": False,
                        }
                    output_lines.append(line)
            except asyncio.TimeoutError:
                timed_out = True

            if timed_out:
                # Send SIGINT to interrupt the running command (not SIGKILL)
                try:
                    if proc.pid:
                        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass

                # Health probe: rebuild session if shell is unresponsive.
                alive = await self._probe_health(proc)
                if not alive:
                    await self._rebuild()

            return {
                "output": _truncate("".join(output_lines)),
                "exit_code": -1,
                "timed_out": timed_out,
            }

    async def execute_background(self, command: str, timeout_seconds: int) -> str:
        """Launch a command in the background and return a task ID."""
        task_id = uuid.uuid4().hex[:12]

        async def _run() -> dict:
            result = await self.execute(command, timeout_seconds)
            self._background_results[task_id] = result
            return result

        t = asyncio.create_task(_run())
        self._background_tasks[task_id] = t
        return task_id

    def get_background_result(self, task_id: str) -> dict | None:
        """Check if a background task has completed."""
        if task_id in self._background_results:
            result = self._background_results.pop(task_id)
            self._background_tasks.pop(task_id, None)
            return result
        task = self._background_tasks.get(task_id)
        if task is None:
            raise ValueError(f"Unknown background task: {task_id}")
        if task.done():
            self._background_tasks.pop(task_id, None)
            return self._background_results.pop(task_id, task.result())
        return None

    async def kill(self) -> str:
        """Kill the persistent shell process."""
        if self._proc is None:
            return "No active shell session"
        # Cancel background tasks
        for tid, task in list(self._background_tasks.items()):
            task.cancel()
        self._background_tasks.clear()
        self._background_results.clear()
        # Kill the shell process
        try:
            if self._proc.pid:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        try:
            self._proc.kill()
        except ProcessLookupError:
            pass
        self._proc = None
        return "Shell session terminated"


class _ShellSessionManager:
    """Per-session shell manager replacing the global singleton.

    Each session is identified by a string key. The default key ``"default"``
    preserves backward compatibility with existing callers.
    """

    _sessions: dict[str, _BashSession] = {}

    @classmethod
    def get(cls, session_id: str = "default") -> _BashSession:
        """Return (or create) the ``_BashSession`` for *session_id*."""
        if session_id not in cls._sessions:
            cls._sessions[session_id] = _BashSession()
        return cls._sessions[session_id]

    @classmethod
    async def kill_all(cls) -> None:
        """Terminate every tracked session and clear the registry."""
        for session in list(cls._sessions.values()):
            await session.kill()
        cls._sessions.clear()


def _truncate(text: str) -> str:
    if len(text) > _MAX_OUTPUT_CHARS:
        return text[:_MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(text)} total chars)"
    return text


@tool(
    name="bash_exec",
    description=(
        "Execute a command in a persistent bash session. "
        "Working directory and environment persist across calls. "
        "Set run_in_background=True for long-running commands."
    ),
    category="system",
    require_confirm=True,
    tags=["dangerous"],
)
async def bash_exec(
    command: str,
    timeout_seconds: int = _DEFAULT_TIMEOUT,
    run_in_background: bool = False,
    description: str = "",
) -> dict:
    """Execute a shell command in a persistent bash session.

    Args:
        command: The shell command to execute.
        timeout_seconds: Maximum execution time (default 120s, max 600s).
        run_in_background: If True, run in background and return a task_id.
        description: Brief description of what the command does.

    Returns:
        Dict with 'output' and 'exit_code', or 'task_id' for background.
    """
    timeout_seconds = min(timeout_seconds, 600)

    # Security: check banned commands
    banned_msg = _check_banned(command)
    if banned_msg:
        return {"output": banned_msg, "exit_code": -2, "timed_out": False}

    session = _ShellSessionManager.get("default")

    if run_in_background:
        task_id = await session.execute_background(command, timeout_seconds)
        return {"task_id": task_id, "status": "running"}

    return await session.execute(command, timeout_seconds)


@tool(
    name="bash_output",
    description="Check the output of a background bash command by task_id.",
    category="system",
    require_confirm=False,
)
def bash_output(task_id: str) -> dict:
    """Get the result of a background bash command.

    Args:
        task_id: The task ID returned by bash_exec with run_in_background=True.

    Returns:
        The command result if finished, or status 'running' if still executing.
    """
    session = _ShellSessionManager.get("default")
    result = session.get_background_result(task_id)
    if result is None:
        return {"status": "running", "task_id": task_id}
    return result


@tool(
    name="kill_shell",
    description="Terminate the persistent shell session and all background tasks.",
    category="system",
    require_confirm=True,
)
async def kill_shell() -> str:
    """Kill the persistent shell process and all background tasks.

    Returns:
        Confirmation message.
    """
    session = _ShellSessionManager.get("default")
    return await session.kill()

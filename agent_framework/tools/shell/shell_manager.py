"""Persistent bash session with security controls.

Manages a single bash subprocess that persists across tool calls,
preserving working directory, environment, and shell state.

Security boundaries:
- Environment whitelist: only safe variables passed to child processes
- Banned command list: blocks network access, privilege escalation, etc.
- Output truncation: prevents oversized results from entering LLM context
- Timeout + health probing: recovers from unresponsive sessions
"""

from __future__ import annotations

import asyncio
import os
import signal
import uuid

DEFAULT_TIMEOUT = 120  # seconds
MAX_OUTPUT_CHARS = 100_000

# Environment variables safe to pass to child processes.
# Secrets (API keys, tokens, credentials) are excluded by design.
ENV_WHITELIST: frozenset[str] = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "TERM",
    "SHELL", "TMPDIR", "TMP", "TEMP",
    "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX",
    "NODE_PATH", "GOPATH", "CARGO_HOME", "RUSTUP_HOME",
})

# Commands banned for security — network access, browsers, privilege escalation
BANNED_COMMANDS: frozenset[str] = frozenset({
    "curl", "wget", "nc", "telnet", "lynx", "w3m", "links",
    "chrome", "firefox", "safari", "aria2c", "axel",
    "sudo", "su", "chmod", "chown", "mount", "umount",
    "iptables", "systemctl", "apt", "yum",
})

# Two-token banned commands (e.g. "pip install", "npm install")
BANNED_TWO_TOKEN_COMMANDS: frozenset[tuple[str, str]] = frozenset({
    ("pip", "install"),
    ("npm", "install"),
})


def build_safe_env() -> dict[str, str]:
    """Build environment with only whitelisted variables.

    Prevents leaking secrets (API keys, tokens, credentials) to child
    processes spawned by the shell tool.
    """
    safe = {k: v for k, v in os.environ.items() if k in ENV_WHITELIST}
    # Always propagate sandbox roots so child processes respect FS boundaries
    roots = os.environ.get("AGENT_FS_SANDBOX_ROOTS")
    if roots:
        safe["AGENT_FS_SANDBOX_ROOTS"] = roots
    return safe


def check_banned(command: str) -> str | None:
    """Return error message if command starts with a banned prefix."""
    tokens = command.strip().split()
    if not tokens:
        return None
    first_token = tokens[0]
    if first_token in BANNED_COMMANDS:
        return (
            f"Command '{first_token}' is blocked for security. "
            "Use web_fetch for HTTP requests."
        )
    if len(tokens) >= 2:
        pair = (tokens[0], tokens[1])
        if pair in BANNED_TWO_TOKEN_COMMANDS:
            label = f"{pair[0]} {pair[1]}"
            return f"Command '{label}' is blocked for security."
    return None


def truncate_output(text: str) -> str:
    """Truncate output to prevent oversized results."""
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(text)} total chars)"
    return text


class BashSession:
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
                env={**build_safe_env(), "TERM": "dumb", "PS1": "", "PS2": ""},
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
        timeout_seconds: int = DEFAULT_TIMEOUT,
    ) -> dict:
        """Execute a command in the persistent bash session.

        Returns dict with 'output', 'exit_code', and 'timed_out' fields.
        """
        async with self._lock:
            proc = await self._ensure_started()
            assert proc.stdin is not None
            assert proc.stdout is not None

            nonce = uuid.uuid4().hex
            wrapped = (
                f"{command}\n"
                f"__exit_code__=$?\n"
                f"printf '\\n{nonce}:%s\\n' \"$__exit_code__\"\n"
            )
            proc.stdin.write(wrapped.encode())
            await proc.stdin.drain()

            output_lines: list[str] = []
            timed_out = False
            try:
                while True:
                    line_bytes = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=timeout_seconds,
                    )
                    if not line_bytes:
                        break
                    line = line_bytes.decode(errors="replace")
                    if line.startswith(nonce):
                        parts = line.strip().split(":")
                        exit_code = int(parts[1]) if len(parts) > 1 else -1
                        return {
                            "output": truncate_output("".join(output_lines)),
                            "exit_code": exit_code,
                            "timed_out": False,
                        }
                    output_lines.append(line)
            except asyncio.TimeoutError:
                timed_out = True

            if timed_out:
                try:
                    if proc.pid:
                        os.killpg(os.getpgid(proc.pid), signal.SIGINT)
                except (ProcessLookupError, OSError):
                    pass
                alive = await self._probe_health(proc)
                if not alive:
                    await self._rebuild()

            return {
                "output": truncate_output("".join(output_lines)),
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
        """Kill the persistent shell process and all background tasks."""
        if self._proc is None:
            return "No active shell session"
        for _tid, task in list(self._background_tasks.items()):
            task.cancel()
        self._background_tasks.clear()
        self._background_results.clear()
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

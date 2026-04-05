"""Docker/Podman container sandbox implementation.

Builds and executes sandboxed commands using OC-style security flags:
--read-only, --cap-drop ALL, --security-opt no-new-privileges,
--pids-limit, --memory, --network none.
"""

from __future__ import annotations

import asyncio
import shutil
import time

from agent_framework.infra.logger import get_logger
from agent_framework.tools.sandbox.protocol import SandboxConfig, SandboxResult

logger = get_logger(__name__)


def build_container_run_args(
    config: SandboxConfig,
    command: str,
    *,
    container_name: str | None = None,
    workspace_dir: str | None = None,
    container_workdir: str = "/workspace",
    env: dict[str, str] | None = None,
) -> list[str]:
    """Build docker/podman run arguments with OC-style security hardening."""
    runtime = config.runtime
    args = [runtime, "run", "--rm"]

    if container_name:
        args.extend(["--name", container_name])

    # Security hardening
    if config.read_only_root:
        args.append("--read-only")

    for cap in config.cap_drop:
        args.extend(["--cap-drop", cap])

    for opt in config.security_opt:
        args.extend(["--security-opt", opt])

    # Resource limits
    args.extend(["--pids-limit", str(config.pids_limit)])
    args.extend(["--memory", config.memory_limit])
    args.extend(["--cpus", str(config.cpus)])

    # Network isolation
    args.extend(["--network", config.network])

    # Tmpfs mounts (writable scratch space in read-only root)
    for tmpfs in config.tmpfs_mounts:
        args.extend(["--tmpfs", tmpfs])

    # Ulimits
    for name, value in config.ulimits.items():
        args.extend(["--ulimit", f"{name}={value}"])

    # Workspace mount
    if workspace_dir and config.workspace_mount_mode != "none":
        suffix = ":ro" if config.workspace_mount_mode == "ro" else ""
        args.extend(["-v", f"{workspace_dir}:{container_workdir}{suffix}"])
        args.extend(["-w", container_workdir])

    # Environment variables
    all_env = dict(config.extra_env)
    if env:
        all_env.update(env)
    for key, value in all_env.items():
        args.extend(["--env", f"{key}={value}"])

    # Image and command
    args.append(config.image)
    args.extend(["sh", "-c", command])

    return args


class ContainerSandbox:
    """Docker/Podman sandbox with OC-style security hardening.

    Creates ephemeral containers for each execution. Containers are
    removed on completion (--rm flag).
    """

    def __init__(
        self,
        config: SandboxConfig,
        workspace_dir: str = "",
    ) -> None:
        self._config = config
        self._workspace_dir = workspace_dir
        self._container_count = 0

    async def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute a command in an ephemeral container."""
        effective_timeout = timeout or self._config.timeout_seconds
        self._container_count += 1
        container_name = f"agent-sandbox-{self._container_count}"

        args = build_container_run_args(
            self._config,
            command,
            container_name=container_name,
            workspace_dir=cwd or self._workspace_dir,
            env=env,
        )

        start = time.monotonic()
        timed_out = False

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
            exit_code = proc.returncode or 0

        except asyncio.TimeoutError:
            timed_out = True
            # Kill the container on timeout
            runtime = self._config.runtime
            kill_proc = await asyncio.create_subprocess_exec(
                runtime, "kill", container_name,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await kill_proc.wait()
            stdout_bytes = b""
            stderr_bytes = b"Execution timed out"
            exit_code = 124  # Standard timeout exit code

        except Exception as e:
            logger.error(
                "sandbox.execution_error",
                command=command[:200],
                error=str(e),
            )
            stdout_bytes = b""
            stderr_bytes = str(e).encode()
            exit_code = 1

        duration_ms = int((time.monotonic() - start) * 1000)

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration_ms=duration_ms,
        )

    async def cleanup(self) -> None:
        """No-op for ephemeral containers (cleaned up by --rm)."""

    def is_available(self) -> bool:
        """Check if the container runtime binary exists on PATH."""
        return shutil.which(self._config.runtime) is not None

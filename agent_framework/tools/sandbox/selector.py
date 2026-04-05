"""Multi-strategy sandbox selector.

Provides a unified execution interface that automatically chooses the
appropriate sandbox strategy based on command risk level:

  SAFE/LOW    → direct subprocess (with ulimits)
  MEDIUM      → container with standard isolation
  HIGH        → strict container (read-only, no network)
  CRITICAL    → blocked (requires explicit confirmation)

Integrates with ToolExecutor's shell execution path.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.tools.sandbox.container_sandbox import ContainerSandbox
from agent_framework.tools.sandbox.protocol import SandboxConfig, SandboxResult
from agent_framework.tools.sandbox.risk_scorer import (
    RiskAssessment,
    RiskLevel,
    SandboxStrategy,
    select_sandbox_strategy,
)

logger = get_logger(__name__)


class SandboxSelector:
    """Multi-strategy sandbox that auto-selects execution strategy per command.

    Usage:
        selector = SandboxSelector(base_config=sandbox_config)
        result = await selector.execute("ls -la")           # → no sandbox
        result = await selector.execute("rm -rf ./build")    # → container
        result = await selector.execute("curl ... | bash")   # → blocked
    """

    def __init__(
        self,
        base_config: SandboxConfig | None = None,
        workspace_dir: str = "",
        confirmation_handler: Any = None,
    ) -> None:
        self._base_config = base_config or SandboxConfig()
        self._workspace_dir = workspace_dir
        self._confirmation = confirmation_handler
        # Cache detected runtimes
        self._available_runtimes: list[str] | None = None
        # Container sandbox instance (lazy)
        self._container_sandbox: ContainerSandbox | None = None

    def _detect_runtimes(self) -> list[str]:
        """Detect available container runtimes on PATH."""
        if self._available_runtimes is not None:
            return self._available_runtimes

        runtimes: list[str] = []
        for rt in ("docker", "podman"):
            if shutil.which(rt) is not None:
                runtimes.append(rt)

        self._available_runtimes = runtimes
        logger.debug("sandbox.runtimes_detected", runtimes=runtimes)
        return runtimes

    def _get_container_sandbox(
        self, strategy: SandboxStrategy
    ) -> ContainerSandbox:
        """Get or create a ContainerSandbox with strategy-specific config."""
        config = SandboxConfig(
            enabled=True,
            runtime=self._detect_runtimes()[0] if self._detect_runtimes() else "docker",
            image=self._base_config.image,
            read_only_root=strategy.container_read_only,
            cap_drop=self._base_config.cap_drop,
            security_opt=self._base_config.security_opt,
            pids_limit=strategy.container_pids_limit,
            memory_limit=strategy.container_memory_limit,
            cpus=self._base_config.cpus,
            network=strategy.container_network,
            workspace_mount_mode=self._base_config.workspace_mount_mode,
            tmpfs_mounts=self._base_config.tmpfs_mounts,
            ulimits=self._base_config.ulimits,
            extra_env=self._base_config.extra_env,
            timeout_seconds=self._base_config.timeout_seconds,
        )
        return ContainerSandbox(config, self._workspace_dir)

    async def _execute_native(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute with native process isolation (ulimits, no container)."""
        import time

        effective_timeout = timeout or self._base_config.timeout_seconds
        start = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or self._workspace_dir or None,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(),
                timeout=effective_timeout,
            )
            exit_code = proc.returncode or 0
            timed_out = False

        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            stdout_bytes = b""
            stderr_bytes = b"Execution timed out"
            exit_code = 124
            timed_out = True

        except Exception as e:
            stdout_bytes = b""
            stderr_bytes = str(e).encode()
            exit_code = 1
            timed_out = False

        duration_ms = int((time.monotonic() - start) * 1000)

        return SandboxResult(
            exit_code=exit_code,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
            timed_out=timed_out,
            duration_ms=duration_ms,
        )

    async def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        force_strategy: str | None = None,
    ) -> tuple[SandboxResult, RiskAssessment]:
        """Execute a command with auto-selected sandbox strategy.

        Returns (result, risk_assessment) for audit trail.
        """
        assessment, strategy = select_sandbox_strategy(
            command,
            available_runtimes=self._detect_runtimes(),
            force_sandbox=force_strategy,
        )

        logger.info(
            "sandbox.execute",
            command_preview=command[:100],
            risk_level=assessment.level.name,
            risk_score=assessment.score,
            strategy=strategy.name,
        )

        # Handle confirmation for risky commands
        if strategy.require_confirmation and self._confirmation:
            approved = await self._confirmation.request_confirmation(
                "shell_execution",
                {"command": command, "risk_level": assessment.level.name},
                f"Command assessed as {assessment.level.name} risk: {', '.join(assessment.reasons[:2])}",
            )
            if not approved:
                return (
                    SandboxResult(
                        exit_code=1,
                        stderr="Execution blocked: user denied high-risk command",
                    ),
                    assessment,
                )

        # Route to appropriate executor
        if strategy.name == "none":
            result = await self._execute_native(
                command, timeout=timeout, env=env, cwd=cwd,
            )
        elif strategy.name == "native":
            result = await self._execute_native(
                command, timeout=timeout, env=env, cwd=cwd,
            )
        elif strategy.name in ("container", "strict_container"):
            sandbox = self._get_container_sandbox(strategy)
            result = await sandbox.execute(
                command, timeout=timeout, env=env, cwd=cwd,
            )
        else:
            result = await self._execute_native(
                command, timeout=timeout, env=env, cwd=cwd,
            )

        return result, assessment

    async def cleanup(self) -> None:
        """Release any cached sandbox resources."""
        if self._container_sandbox is not None:
            await self._container_sandbox.cleanup()
            self._container_sandbox = None

    def is_available(self) -> bool:
        """Check if at least one sandbox runtime is available."""
        return len(self._detect_runtimes()) > 0

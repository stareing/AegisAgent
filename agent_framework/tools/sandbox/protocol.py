"""Sandbox protocol and configuration models.

Defines the contract for sandboxed tool execution environments
and the configuration schema (OC-compatible security hardening).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class SandboxConfig(BaseModel):
    """Container sandbox configuration (OC-style security hardening)."""

    model_config = {"frozen": True}

    enabled: bool = False
    runtime: str = "docker"  # "docker" | "podman" | "none"
    image: str = "python:3.11-slim"
    read_only_root: bool = True
    cap_drop: list[str] = Field(default_factory=lambda: ["ALL"])
    security_opt: list[str] = Field(default_factory=lambda: ["no-new-privileges"])
    pids_limit: int = 256
    memory_limit: str = "512m"
    cpus: float = 1.5
    network: str = "none"  # "none" | "host" | "bridge"
    workspace_mount_mode: str = "rw"  # "rw" | "ro" | "none"
    tmpfs_mounts: list[str] = Field(default_factory=lambda: ["/tmp"])
    ulimits: dict[str, str] = Field(default_factory=lambda: {"nofile": "1024:2048"})
    extra_env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = 120


class SandboxResult(BaseModel):
    """Result from sandboxed command execution."""

    model_config = {"frozen": True}

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0


@runtime_checkable
class SandboxProtocol(Protocol):
    """Contract for sandboxed execution environments."""

    async def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> SandboxResult:
        """Execute a command in the sandbox."""
        ...

    async def cleanup(self) -> None:
        """Release sandbox resources."""
        ...

    def is_available(self) -> bool:
        """Check if the sandbox runtime is available."""
        ...

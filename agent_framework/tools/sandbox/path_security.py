"""Path security — prevents sandbox escape via symlinks and path traversal.

All paths entering the sandbox must be validated against the sandbox root.
Symlink resolution ensures paths cannot escape the root boundary.
"""

from __future__ import annotations

import os
from pathlib import Path


class SandboxPathError(Exception):
    """Raised when a path violates sandbox boundaries."""

    def __init__(self, path: str, root: str, reason: str) -> None:
        self.path = path
        self.root = root
        self.reason = reason
        super().__init__(f"Sandbox path violation: {reason} (path={path}, root={root})")


def is_path_inside(child: str, parent: str) -> bool:
    """Check if child path is inside parent directory (after resolution)."""
    try:
        child_resolved = os.path.realpath(child)
        parent_resolved = os.path.realpath(parent)
        return child_resolved.startswith(parent_resolved + os.sep) or child_resolved == parent_resolved
    except (OSError, ValueError):
        return False


def validate_sandbox_path(
    file_path: str,
    sandbox_root: str,
    *,
    allow_symlinks: bool = False,
) -> str:
    """Validate and resolve a path within sandbox boundaries.

    Returns the resolved absolute path if valid.
    Raises SandboxPathError if the path escapes the sandbox root.
    """
    # Resolve relative to sandbox root
    if not os.path.isabs(file_path):
        file_path = os.path.join(sandbox_root, file_path)

    # Normalize (remove .., ., duplicate separators)
    normalized = os.path.normpath(file_path)

    # Check for obvious traversal before resolution
    if ".." in Path(normalized).parts:
        raise SandboxPathError(
            file_path, sandbox_root,
            "Path contains '..' traversal component",
        )

    # Resolve symlinks and check boundary
    resolved = os.path.realpath(normalized)
    root_resolved = os.path.realpath(sandbox_root)

    if not (resolved.startswith(root_resolved + os.sep) or resolved == root_resolved):
        raise SandboxPathError(
            file_path, sandbox_root,
            "Resolved path escapes sandbox root",
        )

    # Symlink check (more strict: check each component)
    if not allow_symlinks:
        current = Path(root_resolved)
        relative = Path(resolved).relative_to(root_resolved)
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise SandboxPathError(
                    file_path, sandbox_root,
                    f"Symlink detected at {current}",
                )

    return resolved


def map_container_path_to_host(
    container_path: str,
    container_workdir: str,
    host_workspace: str,
) -> str:
    """Map a container-internal path back to the host filesystem.

    Used when the sandbox reports file paths that need host-side access.
    """
    if container_path.startswith(container_workdir):
        relative = container_path[len(container_workdir):].lstrip("/")
        return os.path.join(host_workspace, relative)
    return container_path

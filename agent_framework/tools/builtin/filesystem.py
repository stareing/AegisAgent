"""Built-in filesystem tools.

These tools provide basic file system operations.
All require confirmation by default (require_confirm=True) for safety.
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_framework.tools.decorator import tool

_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB
_MAX_GLOB_RESULTS = 500

_SENSITIVE_PATTERNS: set[str] = {
    ".env",
    ".pem",
    ".key",
    ".ssh",
    ".git/config",
}


def _sandbox_roots() -> list[Path]:
    """FS sandbox roots from env, defaulting to cwd when unset."""
    raw = os.environ.get("AGENT_FS_SANDBOX_ROOTS", "").strip()
    if not raw:
        return [Path.cwd().resolve()]
    roots: list[Path] = []
    for part in raw.split(os.pathsep):
        if not part.strip():
            continue
        roots.append(Path(part).expanduser().resolve())
    return roots


def _ensure_within_sandbox(path: Path) -> Path:
    """Enforce path is within configured sandbox roots."""
    resolved = path.expanduser().resolve()
    roots = _sandbox_roots()
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise PermissionError(
        f"Path '{resolved}' is outside sandbox roots: "
        + ", ".join(str(r) for r in roots)
    )


def _is_sensitive_path(file_path: Path) -> bool:
    """Check whether the path matches a known sensitive pattern."""
    path_str = str(file_path)
    name = file_path.name
    for pattern in _SENSITIVE_PATTERNS:
        if name == pattern or path_str.endswith(pattern):
            return True
        # Also match suffix patterns like .pem, .key, .env
        if pattern.startswith(".") and "/" not in pattern and name.endswith(pattern):
            return True
    return False


def _is_binary_file(file_path: Path, check_bytes: int = 8192) -> bool:
    """Heuristic: file is binary if initial bytes contain null characters."""
    with open(file_path, "rb") as f:
        chunk = f.read(check_bytes)
    return b"\x00" in chunk


@tool(
    name="read_file",
    description="Read the contents of a file at the given path.",
    category="filesystem",
    require_confirm=False,
)
def read_file(path: str, encoding: str = "utf-8") -> str:
    """Read the contents of a file at the given path.

    Args:
        path: The file path to read.
        encoding: File encoding, defaults to utf-8.

    Returns:
        The file contents as a string.
    """
    file_path = _ensure_within_sandbox(Path(path))
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")

    file_size = file_path.stat().st_size
    if file_size > _MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"File too large ({file_size} bytes). "
            f"Maximum allowed size is {_MAX_FILE_SIZE_BYTES} bytes (2 MB)."
        )

    if _is_binary_file(file_path):
        raise ValueError(
            f"File appears to be binary: {path}. "
            "Only text files can be read with this tool."
        )

    if _is_sensitive_path(file_path):
        raise PermissionError(
            f"File matches a sensitive pattern and requires confirmation: {path}. "
            "Matching patterns: " + ", ".join(sorted(_SENSITIVE_PATTERNS))
        )

    return file_path.read_text(encoding=encoding)


@tool(
    name="write_file",
    description="Write content to a file at the given path. Creates the file if it doesn't exist.",
    category="filesystem",
    require_confirm=True,
)
def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """Write content to a file.

    Args:
        path: The file path to write to.
        content: The content to write.
        encoding: File encoding, defaults to utf-8.

    Returns:
        Confirmation message with bytes written.
    """
    file_path = _ensure_within_sandbox(Path(path))
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding=encoding)
    return f"Written {len(content)} characters to {path}"


@tool(
    name="list_directory",
    description="List files and directories at the given path.",
    category="filesystem",
    require_confirm=False,
)
def list_directory(path: str = ".", pattern: str = "*") -> list[str]:
    """List files and directories.

    Args:
        path: Directory path to list.
        pattern: Glob pattern to filter results.

    Returns:
        List of file/directory names.
    """
    if "**" in pattern:
        raise ValueError(
            "Recursive glob pattern '**' is not allowed. "
            "Use a non-recursive pattern instead."
        )

    dir_path = _ensure_within_sandbox(Path(path))
    if not dir_path.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not dir_path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    results: list[str] = []
    for p in dir_path.glob(pattern):
        results.append(str(p.relative_to(dir_path)))
        if len(results) >= _MAX_GLOB_RESULTS:
            break

    return sorted(results)


@tool(
    name="file_exists",
    description="Check if a file or directory exists at the given path.",
    category="filesystem",
    require_confirm=False,
)
def file_exists(path: str) -> bool:
    """Check if a path exists.

    Args:
        path: The path to check.

    Returns:
        True if the path exists.
    """
    try:
        return _ensure_within_sandbox(Path(path)).exists()
    except PermissionError:
        return False

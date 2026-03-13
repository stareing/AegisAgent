"""Built-in filesystem tools.

These tools provide basic file system operations.
All require confirmation by default (require_confirm=True) for safety.
"""

from __future__ import annotations

from pathlib import Path

from agent_framework.tools.decorator import tool


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
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")
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
    file_path = Path(path)
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
    dir_path = Path(path)
    if not dir_path.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not dir_path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")
    return sorted(str(p.relative_to(dir_path)) for p in dir_path.glob(pattern))


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
    return Path(path).exists()

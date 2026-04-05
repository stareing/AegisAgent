"""Built-in filesystem tools.

Category: filesystem — read operations are safe for sub-agents,
write operations require confirmation by default.

These tools provide basic file system operations with sandbox enforcement.
"""

from __future__ import annotations

import difflib
import os
from pathlib import Path
from typing import Optional

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB
_MAX_GLOB_RESULTS = 500
_BINARY_CHECK_BYTES = 8192

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


def _is_binary_file(file_path: Path, check_bytes: int = _BINARY_CHECK_BYTES) -> bool:
    """Heuristic: file is binary if initial bytes contain null characters."""
    with open(file_path, "rb") as f:
        chunk = f.read(check_bytes)
    return b"\x00" in chunk


def _detect_line_ending(content: bytes) -> str:
    """Detect the dominant line ending in raw bytes.

    Returns '\\r\\n' if CRLF is present, otherwise '\\n'.
    """
    crlf_count = content.count(b"\r\n")
    # LF count excluding those that are part of CRLF
    lf_count = content.count(b"\n") - crlf_count
    if crlf_count > lf_count:
        return "\r\n"
    return "\n"


def _format_lines_with_numbers(lines: list[str], start: int = 1) -> str:
    """Format lines with right-aligned line numbers (cat -n style)."""
    if not lines:
        return ""
    width = len(str(start + len(lines) - 1))
    parts: list[str] = []
    for i, line in enumerate(lines):
        line_no = start + i
        parts.append(f"{line_no:>{width}}\t{line}")
    return "\n".join(parts)


def _is_gitignored_path(file_path: Path) -> bool:
    """Check if a path is gitignored by walking up to find .gitignore.

    Lightweight check — imports search module utilities lazily to avoid
    circular imports at module level.
    """
    try:
        from agent_framework.tools.builtin.search import (
            _is_gitignored,
            _load_gitignore_rules,
        )
    except ImportError:
        return False

    # Find repo root by looking for .git directory
    current = file_path.parent
    repo_root: Path | None = None
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            repo_root = parent
            break

    if repo_root is None:
        return False

    rules = _load_gitignore_rules(str(repo_root))
    if not rules:
        return False

    try:
        rel = str(file_path.relative_to(repo_root))
    except ValueError:
        return False

    return _is_gitignored(rel, is_dir=False, rules=rules)


# ── Tools ────────────────────────────────────────────────────


@tool(
    name="read_file",
    description=(
        "Read the contents of a file at the given path. "
        "Supports optional line range selection with start_line/end_line."
    ),
    category="filesystem",
    require_confirm=False,
    tags=["system", "file", "read"],
    namespace=SYSTEM_NAMESPACE,
)
def read_file(
    path: str,
    encoding: str = "utf-8",
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> str:
    """Read the contents of a file at the given path.

    Args:
        path: The file path to read.
        encoding: File encoding, defaults to utf-8.
        start_line: 1-based start line number (inclusive). If omitted, reads from the beginning.
        end_line: 1-based end line number (inclusive). If omitted, reads to the end.

    Returns:
        The file contents with line numbers in cat -n format.
    """
    file_path = _ensure_within_sandbox(Path(path))
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {path}")

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

    file_size = file_path.stat().st_size

    # Truncation warning for large files when no line range is specified
    if file_size > _MAX_FILE_SIZE_BYTES and start_line is None and end_line is None:
        # Count lines without loading entire file into memory
        line_count = 0
        with open(file_path, "r", encoding=encoding, errors="replace") as f:
            for _ in f:
                line_count += 1
        return (
            f"File is too large to display in full ({file_size:,} bytes, {line_count:,} lines). "
            f"Use start_line and end_line parameters to read specific sections. "
            f"For example: start_line=1, end_line=100"
        )

    all_lines = file_path.read_text(encoding=encoding).splitlines()
    total_lines = len(all_lines)

    # Apply line range selection
    if start_line is not None or end_line is not None:
        effective_start = max(1, start_line if start_line is not None else 1)
        effective_end = min(total_lines, end_line if end_line is not None else total_lines)

        if effective_start > total_lines:
            return f"start_line {effective_start} exceeds total line count ({total_lines})."

        # Convert to 0-based indexing for slicing
        selected = all_lines[effective_start - 1 : effective_end]
        return _format_lines_with_numbers(selected, start=effective_start)

    return _format_lines_with_numbers(all_lines, start=1)


@tool(
    name="write_file",
    description=(
        "Write content to a file at the given path. Creates the file and parent "
        "directories if they don't exist. Returns a unified diff and change stats."
    ),
    category="filesystem",
    require_confirm=True,
    tags=["system", "file", "write"],
    namespace=SYSTEM_NAMESPACE,
)
def write_file(path: str, content: str, encoding: str = "utf-8") -> str:
    """Write content to a file.

    Detects and preserves line endings from existing files. Generates a unified
    diff and reports lines added, removed, and changed.

    Args:
        path: The file path to write to.
        content: The content to write.
        encoding: File encoding, defaults to utf-8.

    Returns:
        Confirmation message with diff and change statistics.
    """
    file_path = _ensure_within_sandbox(Path(path))
    file_path.parent.mkdir(parents=True, exist_ok=True)

    old_content = ""
    detected_ending = "\n"
    is_new_file = True

    if file_path.exists() and file_path.is_file():
        is_new_file = False
        raw_bytes = file_path.read_bytes()
        detected_ending = _detect_line_ending(raw_bytes)
        old_content = raw_bytes.decode(encoding, errors="replace")

    # Normalize content to use the detected line ending
    normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
    if detected_ending == "\r\n":
        write_content = normalized_content.replace("\n", "\r\n")
    else:
        write_content = normalized_content

    file_path.write_text(write_content, encoding=encoding)

    # Generate unified diff
    old_lines = old_content.splitlines(keepends=True)
    new_lines = write_content.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )

    # Compute diff stats
    lines_added = 0
    lines_removed = 0
    for line in diff_lines:
        if line.startswith("+") and not line.startswith("+++"):
            lines_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            lines_removed += 1
    lines_changed = min(lines_added, lines_removed)
    net_added = lines_added - lines_changed
    net_removed = lines_removed - lines_changed

    # Build result message
    parts: list[str] = []
    if is_new_file:
        parts.append(f"Created new file: {path}")
    else:
        parts.append(f"Updated file: {path}")

    parts.append(f"Written {len(write_content)} characters ({len(write_content.encode(encoding))} bytes)")
    parts.append(f"Stats: +{net_added} added, -{net_removed} removed, ~{lines_changed} changed")

    if diff_lines:
        diff_text = "\n".join(diff_lines)
        # Limit diff output to avoid overwhelming context
        max_diff_chars = 4000
        if len(diff_text) > max_diff_chars:
            diff_text = diff_text[:max_diff_chars] + "\n... (diff truncated)"
        parts.append(f"\nDiff:\n{diff_text}")
    else:
        parts.append("No changes detected (file content identical).")

    return "\n".join(parts)


@tool(
    name="read_many_files",
    description=(
        "Read multiple files at once, concatenated with path separators. "
        "Uses water-filling budget allocation to fairly distribute characters "
        "across files. Skips binary and unreadable files with warnings."
    ),
    category="filesystem",
    require_confirm=False,
    tags=["system", "file", "read"],
    namespace=SYSTEM_NAMESPACE,
)
def read_many_files(
    paths: list[str],
    max_total_chars: int = 200_000,
) -> str:
    """Read multiple files and concatenate with path separators.

    Args:
        paths: List of file paths to read.
        max_total_chars: Maximum total characters across all files (default 200000).

    Returns:
        Concatenated file contents with separators, or error summaries for
        files that could not be read.
    """
    if not paths:
        return "No paths provided."

    # Validate and resolve all paths first
    resolved: list[tuple[str, Path]] = []
    warnings: list[str] = []

    for raw_path in paths:
        try:
            file_path = _ensure_within_sandbox(Path(raw_path))
        except PermissionError as exc:
            warnings.append(f"SKIP {raw_path}: {exc}")
            continue

        if not file_path.exists():
            warnings.append(f"SKIP {raw_path}: file not found")
            continue

        if not file_path.is_file():
            warnings.append(f"SKIP {raw_path}: not a file")
            continue

        if _is_binary_file(file_path):
            warnings.append(f"SKIP {raw_path}: binary file")
            continue

        if _is_sensitive_path(file_path):
            warnings.append(f"SKIP {raw_path}: sensitive file pattern")
            continue

        if _is_gitignored_path(file_path):
            warnings.append(f"SKIP {raw_path}: gitignored")
            continue

        resolved.append((raw_path, file_path))

    if not resolved:
        result_parts = ["No readable files found."]
        if warnings:
            result_parts.extend(warnings)
        return "\n".join(result_parts)

    # Water-filling budget allocation: allocate chars fairly, smallest first.
    # 1. Read file sizes (in chars — approximate via byte size).
    file_sizes: list[tuple[str, Path, int]] = []
    for raw_path, file_path in resolved:
        byte_size = file_path.stat().st_size
        file_sizes.append((raw_path, file_path, byte_size))

    # Sort by size ascending for water-filling
    file_sizes.sort(key=lambda t: t[2])

    n = len(file_sizes)
    remaining_budget = max_total_chars
    allocations: dict[str, int] = {}

    for i, (raw_path, _file_path, size) in enumerate(file_sizes):
        files_left = n - i
        fair_share = remaining_budget // files_left
        alloc = min(size, fair_share)
        allocations[raw_path] = alloc
        remaining_budget -= alloc

    # Read files in original order with allocated budgets
    output_parts: list[str] = []

    if warnings:
        output_parts.extend(warnings)
        output_parts.append("")

    for raw_path, file_path in resolved:
        budget = allocations[raw_path]
        separator = f"--- {raw_path} ---"
        output_parts.append(separator)

        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            output_parts.append(f"[Error reading file: {exc}]")
            continue

        if len(content) > budget:
            truncated = content[:budget]
            # Avoid cutting mid-line — trim to last newline
            last_nl = truncated.rfind("\n")
            if last_nl > 0:
                truncated = truncated[: last_nl + 1]
            output_parts.append(truncated)
            output_parts.append(
                f"[Truncated: showing {len(truncated)}/{len(content)} chars. "
                f"Use read_file with start_line/end_line for full content.]"
            )
        else:
            output_parts.append(content)

    return "\n".join(output_parts)


@tool(
    name="ask_user",
    description=(
        "Ask the user a question and wait for their response. "
        "Use this when you need clarification or a decision from the user."
    ),
    category="interaction",
    require_confirm=False,
    tags=["system", "interaction"],
    namespace=SYSTEM_NAMESPACE,
)
def ask_user(
    question: str,
    options: Optional[list[str]] = None,
    default: Optional[str] = None,
) -> str:
    """Ask the user a question.

    The actual user-facing prompt is handled by the ConfirmationHandler layer.
    This tool returns the question text so the framework can present it.

    Args:
        question: The question to ask the user.
        options: Optional list of choices for the user to pick from.
        default: Optional default answer if the user provides no input.

    Returns:
        The formatted question text for the ConfirmationHandler to present.
    """
    parts: list[str] = [question]

    if options:
        parts.append("Options: " + ", ".join(options))

    if default is not None:
        parts.append(f"Default: {default}")

    return "\n".join(parts)


@tool(
    name="list_directory",
    description="List files and directories at the given path.",
    category="filesystem",
    require_confirm=False,
    tags=["system", "file", "read"],
    namespace=SYSTEM_NAMESPACE,
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
    tags=["system", "file", "read"],
    namespace=SYSTEM_NAMESPACE,
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

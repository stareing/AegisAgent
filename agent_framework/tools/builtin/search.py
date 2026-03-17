"""Built-in search tools.

Provides regex content search (grep_search) and recursive file pattern
matching (glob_files).

Both tools respect .gitignore: files and directories matched by
.gitignore rules are excluded by default. This avoids searching
through build outputs, dependencies, and other generated content.
"""

from __future__ import annotations

import fnmatch
import os
import re
from functools import lru_cache
from pathlib import Path

from agent_framework.tools.builtin.filesystem import _ensure_within_sandbox
from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

# Sane default limits to avoid overwhelming the LLM context.
_MAX_MATCHES = 200
_MAX_FILES = 500

# Always skip these directories even without a .gitignore
_ALWAYS_SKIP_DIRS = {".git"}


# ── .gitignore parsing ─────────────────────────────────────


def _parse_gitignore(root: Path) -> list[tuple[str, bool]]:
    """Parse .gitignore at *root* into (pattern, is_negation) pairs.

    Handles:
    - Comments (#) and blank lines (skipped)
    - Negation patterns (! prefix)
    - Directory-only patterns (trailing /)
    - Rooted patterns (leading /)
    - Double-star wildcards (**) converted to fnmatch-compatible form
    """
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return []

    rules: list[tuple[str, bool]] = []
    try:
        lines = gitignore.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        negated = False
        if line.startswith("!"):
            negated = True
            line = line[1:]

        # Trailing slash means directory-only — we match it without the slash
        # since os.walk gives us directory names without trailing separator.
        if line.endswith("/"):
            line = line.rstrip("/")

        # Leading slash means anchored to repo root — we store as-is;
        # _is_gitignored treats patterns without "/" as basename-only.
        rules.append((line, negated))

    return rules


@lru_cache(maxsize=64)
def _load_gitignore_rules(root: str) -> list[tuple[str, bool]]:
    """Cached wrapper so repeated searches in the same root don't re-parse."""
    return _parse_gitignore(Path(root))


def _is_gitignored(
    rel_path: str,
    is_dir: bool,
    rules: list[tuple[str, bool]],
) -> bool:
    """Check whether *rel_path* (relative to repo root) is ignored.

    Mimics core gitignore semantics:
    - A pattern without '/' matches against the basename only.
    - A pattern with '/' matches against the full relative path.
    - '**' in patterns matches any number of path segments.
    - Negation ('!') un-ignores a previously ignored path.
    """
    basename = os.path.basename(rel_path)
    ignored = False

    for pattern, negated in rules:
        # Determine whether to match against basename or full path
        if "/" in pattern.rstrip("/"):
            # Path-anchored pattern: match against full relative path
            match_target = rel_path
            match_pattern = pattern.lstrip("/")
        else:
            # Basename-only pattern
            match_target = basename
            match_pattern = pattern

        # Convert ** globs to fnmatch-compatible patterns
        match_pattern = match_pattern.replace("**/", "*")
        match_pattern = match_pattern.replace("/**", "*")

        if fnmatch.fnmatch(match_target, match_pattern):
            ignored = not negated

    return ignored


def _find_repo_root(start: Path) -> Path | None:
    """Walk up from *start* to find the nearest directory containing .git/."""
    current = start if start.is_dir() else start.parent
    for parent in [current, *current.parents]:
        if (parent / ".git").exists():
            return parent
    return None


# ── File collection ────────────────────────────────────────


def _collect_files(
    root: Path,
    glob_filter: str | None,
    respect_gitignore: bool = True,
) -> list[Path]:
    """Collect files under root, filtered by glob and .gitignore rules."""
    repo_root = _find_repo_root(root) if respect_gitignore else None
    rules = _load_gitignore_rules(str(repo_root)) if repo_root else []

    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)

        # Always prune .git
        dirnames[:] = [d for d in dirnames if d not in _ALWAYS_SKIP_DIRS]

        # Prune gitignored directories
        if rules and repo_root:
            surviving: list[str] = []
            for d in dirnames:
                rel = str((dp / d).relative_to(repo_root))
                if not _is_gitignored(rel, is_dir=True, rules=rules):
                    surviving.append(d)
            dirnames[:] = surviving

        for name in filenames:
            full = dp / name

            # .gitignore file-level check
            if rules and repo_root:
                rel = str(full.relative_to(repo_root))
                if _is_gitignored(rel, is_dir=False, rules=rules):
                    continue

            # Glob filter
            if glob_filter:
                if not fnmatch.fnmatch(name, glob_filter):
                    rel_from_root = os.path.relpath(str(full), str(root))
                    if not fnmatch.fnmatch(rel_from_root, glob_filter):
                        continue

            files.append(full)

            if len(files) >= _MAX_FILES:
                return files

    return files


def _is_hidden_or_gitignored(path: Path, root: Path) -> bool:
    """Check whether path should be excluded from glob_files results."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    # Hidden path component (dot-prefixed)
    if any(part.startswith(".") for part in rel.parts):
        return True
    # .gitignore check
    repo_root = _find_repo_root(root)
    if repo_root:
        rules = _load_gitignore_rules(str(repo_root))
        if rules:
            repo_rel = str(path.relative_to(repo_root))
            return _is_gitignored(repo_rel, is_dir=False, rules=rules)
    return False


# ── Tools ──────────────────────────────────────────────────


@tool(
    name="grep_search",
    description=(
        "Search file contents using a regular expression pattern. "
        "Returns matching lines with file paths and line numbers. "
        "Supports glob-based file filtering and case-insensitive search. "
        "Files and directories matched by .gitignore are excluded by default."
    ),
    category="filesystem",
    require_confirm=False,
    tags=["system", "search", "read"],
    namespace=SYSTEM_NAMESPACE,
)
def grep_search(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_insensitive: bool = False,
    context_lines: int = 0,
    max_results: int = 50,
    include_gitignored: bool = False,
) -> dict:
    """Search file contents with regex.

    Args:
        pattern: Regular expression pattern to search for.
        path: File or directory to search in (default: current directory).
        glob: Glob pattern to filter files (e.g. '*.py', '**/*.ts').
        case_insensitive: If True, ignore case when matching.
        context_lines: Number of context lines before and after each match.
        max_results: Maximum number of matches to return.
        include_gitignored: If True, also search files ignored by .gitignore.

    Returns:
        Dict with 'matches' list and 'total_matches' count.
    """
    max_results = min(max_results, _MAX_MATCHES)
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}")

    root = _ensure_within_sandbox(Path(path))

    if root.is_file():
        files = [root]
    elif root.is_dir():
        files = _collect_files(root, glob, respect_gitignore=not include_gitignored)
    else:
        raise FileNotFoundError(f"Path not found: {path}")

    matches: list[dict] = []
    total = 0

    for file_path in files:
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, UnicodeDecodeError):
            continue

        for i, line in enumerate(lines, start=1):
            if compiled.search(line):
                total += 1
                if len(matches) >= max_results:
                    continue
                entry: dict = {
                    "file": str(file_path),
                    "line": i,
                    "content": line.rstrip(),
                }
                if context_lines > 0:
                    start = max(0, i - 1 - context_lines)
                    end = min(len(lines), i + context_lines)
                    entry["context_before"] = [
                        l.rstrip() for l in lines[start : i - 1]
                    ]
                    entry["context_after"] = [
                        l.rstrip() for l in lines[i:end]
                    ]
                matches.append(entry)

    return {"matches": matches, "total_matches": total}


@tool(
    name="glob_files",
    description=(
        "Find files by glob pattern (e.g. '**/*.py', 'src/**/*.ts'). "
        "Returns matching file paths sorted by modification time (newest first). "
        "Files and directories matched by .gitignore are excluded by default."
    ),
    category="filesystem",
    require_confirm=False,
    tags=["system", "search", "read"],
    namespace=SYSTEM_NAMESPACE,
)
def glob_files(
    pattern: str,
    path: str = ".",
    max_results: int = 100,
    include_gitignored: bool = False,
) -> list[str]:
    """Find files matching a glob pattern.

    Args:
        pattern: Glob pattern (e.g. '**/*.py', 'src/**/*.ts').
        path: Root directory to search from.
        max_results: Maximum number of results to return.
        include_gitignored: If True, include files ignored by .gitignore.

    Returns:
        List of matching file paths, sorted by modification time (newest first).
    """
    max_results = min(max_results, _MAX_FILES)
    root = _ensure_within_sandbox(Path(path))
    if not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {path}")

    found: list[tuple[float, str]] = []
    for match in root.glob(pattern):
        if not match.is_file():
            continue
        if not include_gitignored and _is_hidden_or_gitignored(match, root):
            continue
        try:
            mtime = match.stat().st_mtime
        except OSError:
            mtime = 0.0
        found.append((mtime, str(match)))

    found.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in found[:max_results]]

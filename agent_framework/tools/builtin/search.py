"""Built-in search tools.

Provides regex content search (grep_search) and recursive file pattern
matching (glob_files).

Both tools respect .gitignore: files and directories matched by
.gitignore rules are excluded by default. This avoids searching
through build outputs, dependencies, and other generated content.

grep_search tries `git grep` first when inside a git repo (faster),
falling back to a Python-based file walk when git is unavailable or
the target path is outside a git repository.
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
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
    exclude_pattern: str | None = None,
) -> list[Path]:
    """Collect files under root, filtered by glob and .gitignore rules."""
    repo_root = _find_repo_root(root) if respect_gitignore else None
    rules = _load_gitignore_rules(str(repo_root)) if repo_root else []

    exclude_re = None
    if exclude_pattern:
        try:
            exclude_re = re.compile(exclude_pattern)
        except re.error:
            # Fall back to treating it as a glob
            exclude_re = None

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

            # Exclude pattern filter
            if exclude_re and exclude_re.search(str(full)):
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


# ── git grep helper ────────────────────────────────────────


def _try_git_grep(
    pattern: str,
    root: Path,
    repo_root: Path,
    case_insensitive: bool,
    context_lines: int,
    max_results: int,
    glob_filter: str | None,
    exclude_pattern: str | None,
    names_only: bool,
) -> dict | None:
    """Attempt to run git grep and parse results.

    Returns a result dict on success, or None if git grep is unavailable
    or fails (triggering fallback to Python walk).
    """
    cmd: list[str] = ["git", "grep", "--no-color", "-n"]

    if case_insensitive:
        cmd.append("-i")

    if names_only:
        cmd.append("-l")

    if context_lines > 0 and not names_only:
        cmd.extend(["-C", str(context_lines)])

    if max_results > 0 and names_only:
        cmd.extend(["-m", str(max_results)])

    # Use -E for extended regex (closer to Python re behavior)
    cmd.extend(["-E", "-e", pattern])

    # Restrict search to a subdirectory if root is not the repo root
    pathspec: str | None = None
    try:
        rel = root.relative_to(repo_root)
        if str(rel) != ".":
            pathspec = str(rel)
    except ValueError:
        return None

    # Apply glob filter as a pathspec
    if glob_filter:
        if pathspec:
            cmd.append("--")
            cmd.append(f"{pathspec}/{glob_filter}")
        else:
            cmd.append("--")
            cmd.append(glob_filter)
    elif pathspec:
        cmd.append("--")
        cmd.append(pathspec)

    # Apply exclude pattern
    if exclude_pattern:
        cmd.extend([":!"+exclude_pattern])

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        # git not available or timed out — fall back
        return None

    # git grep exits 1 when no matches found (not an error)
    if proc.returncode not in (0, 1):
        return None

    if names_only:
        return _parse_git_grep_names_only(proc.stdout, repo_root, max_results)

    return _parse_git_grep_output(proc.stdout, repo_root, context_lines, max_results)


def _parse_git_grep_names_only(
    output: str,
    repo_root: Path,
    max_results: int,
) -> dict:
    """Parse git grep -l output into file path list."""
    files: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        full_path = str(repo_root / line)
        files.append(full_path)
        if len(files) >= max_results:
            break

    return {
        "matches": [{"file": f} for f in files],
        "total_matches": len(files),
    }


def _parse_git_grep_output(
    output: str,
    repo_root: Path,
    context_lines: int,
    max_results: int,
) -> dict:
    """Parse git grep -n output into structured match dicts."""
    matches: list[dict] = []
    total = 0

    if context_lines > 0:
        # With context, git grep uses -- as block separator
        return _parse_git_grep_context_output(output, repo_root, context_lines, max_results)

    for line in output.splitlines():
        # Format: file:line_no:content
        # Use maxsplit=2 to handle colons in content
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue

        file_rel, line_no_str, content = parts
        try:
            line_no = int(line_no_str)
        except ValueError:
            continue

        total += 1
        if len(matches) < max_results:
            matches.append({
                "file": str(repo_root / file_rel),
                "line": line_no,
                "content": content.rstrip(),
            })

    return {"matches": matches, "total_matches": total}


def _parse_git_grep_context_output(
    output: str,
    repo_root: Path,
    context_lines: int,
    max_results: int,
) -> dict:
    """Parse git grep output with context lines (-C).

    Context output uses:
    - file:line_no:content  for matching lines
    - file-line_no-content  for context lines
    - --                    as block separator
    """
    matches: list[dict] = []
    total = 0

    # Split into blocks separated by --
    blocks = output.split("\n--\n")

    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue

        match_line_entry: dict | None = None
        context_before: list[str] = []
        context_after: list[str] = []
        found_match = False

        for raw_line in lines:
            # Try matching line (file:line_no:content)
            match_parts = raw_line.split(":", 2)
            context_parts = raw_line.split("-", 2)

            is_match_line = False
            if len(match_parts) >= 3:
                try:
                    int(match_parts[1])
                    is_match_line = True
                except ValueError:
                    pass

            if is_match_line and len(match_parts) >= 3:
                file_rel, line_no_str, content = match_parts
                try:
                    line_no = int(line_no_str)
                except ValueError:
                    continue

                if not found_match:
                    # First match in block — this is the primary match
                    found_match = True
                    total += 1
                    match_line_entry = {
                        "file": str(repo_root / file_rel),
                        "line": line_no,
                        "content": content.rstrip(),
                    }
                else:
                    # Additional match in context — treat as context_after
                    context_after.append(content.rstrip())
            elif len(context_parts) >= 3:
                # Context line (file-line_no-content)
                try:
                    int(context_parts[1])
                    content = context_parts[2]
                    if found_match:
                        context_after.append(content.rstrip())
                    else:
                        context_before.append(content.rstrip())
                except ValueError:
                    pass

        if match_line_entry and len(matches) < max_results:
            if context_before:
                match_line_entry["context_before"] = context_before
            if context_after:
                match_line_entry["context_after"] = context_after
            matches.append(match_line_entry)

    return {"matches": matches, "total_matches": total}


# ── Tools ──────────────────────────────────────────────────


@tool(
    name="grep_search",
    description=(
        "Search file contents using a regular expression pattern. "
        "Returns matching lines with file paths and line numbers. "
        "Supports glob-based file filtering and case-insensitive search. "
        "Files and directories matched by .gitignore are excluded by default. "
        "Uses git grep for speed inside git repos, with Python fallback."
    ),
    category="filesystem",
    require_confirm=False,
    tags=["system", "search", "read"],
    namespace=SYSTEM_NAMESPACE,
    concurrency_class="concurrent_safe",
    is_read_only=True,
    search_hint="search file contents regex pattern grep ripgrep",
    activity_description="Searching files",
    prompt="Search for patterns in file contents using regex. Supports glob filtering and context lines.",
    tool_use_summary_tpl="Searched {pattern} in {path}",
)
def grep_search(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_insensitive: bool = False,
    context_lines: int = 0,
    max_results: int = 50,
    include_gitignored: bool = False,
    names_only: bool = False,
    exclude_pattern: str | None = None,
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
        names_only: If True, return only file paths (no line content).
        exclude_pattern: Regex pattern for files/paths to exclude from search.

    Returns:
        Dict with 'matches' list and 'total_matches' count.
        When names_only=True, each match only contains 'file'.
    """
    max_results = min(max_results, _MAX_MATCHES)
    flags = re.IGNORECASE if case_insensitive else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        raise ValueError(f"Invalid regex pattern: {e}")

    root = _ensure_within_sandbox(Path(path))

    if root.is_file():
        # Single file — skip git grep, use Python directly
        return _python_grep_single_file(
            root, compiled, context_lines, max_results, names_only,
        )

    if not root.is_dir():
        raise FileNotFoundError(f"Path not found: {path}")

    # Try git grep first (faster for large repos) when not including gitignored files
    if not include_gitignored:
        repo_root = _find_repo_root(root)
        if repo_root is not None:
            result = _try_git_grep(
                pattern=pattern,
                root=root,
                repo_root=repo_root,
                case_insensitive=case_insensitive,
                context_lines=context_lines,
                max_results=max_results,
                glob_filter=glob,
                exclude_pattern=exclude_pattern,
                names_only=names_only,
            )
            if result is not None:
                return result

    # Fallback: Python-based file walk
    files = _collect_files(
        root, glob,
        respect_gitignore=not include_gitignored,
        exclude_pattern=exclude_pattern,
    )

    if names_only:
        return _python_grep_names_only(files, compiled, max_results)

    return _python_grep_full(files, compiled, context_lines, max_results)


def _python_grep_single_file(
    file_path: Path,
    compiled: re.Pattern,
    context_lines: int,
    max_results: int,
    names_only: bool,
) -> dict:
    """Search a single file with Python regex."""
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeDecodeError):
        return {"matches": [], "total_matches": 0}

    if names_only:
        for line in lines:
            if compiled.search(line):
                return {
                    "matches": [{"file": str(file_path)}],
                    "total_matches": 1,
                }
        return {"matches": [], "total_matches": 0}

    matches: list[dict] = []
    total = 0
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


def _python_grep_names_only(
    files: list[Path],
    compiled: re.Pattern,
    max_results: int,
) -> dict:
    """Search files and return only file paths that contain a match."""
    matching_files: list[dict] = []

    for file_path in files:
        if len(matching_files) >= max_results:
            break
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            continue

        if compiled.search(text):
            matching_files.append({"file": str(file_path)})

    return {"matches": matching_files, "total_matches": len(matching_files)}


def _python_grep_full(
    files: list[Path],
    compiled: re.Pattern,
    context_lines: int,
    max_results: int,
) -> dict:
    """Full Python grep with line content and optional context."""
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
    concurrency_class="concurrent_safe",
    is_read_only=True,
    search_hint="find files by name pattern glob",
    activity_description="Finding files",
    prompt="Find files matching glob patterns. Returns paths sorted by modification time.",
    tool_use_summary_tpl="Found files matching {pattern}",
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

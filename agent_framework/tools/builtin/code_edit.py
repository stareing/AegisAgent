"""Built-in code editing tools.

Provides 4-stage cascade find-and-replace editing (edit_file) and Jupyter notebook
cell editing (notebook_edit).

Edit matching stages (inspired by Gemini CLI):
  Stage 1 - EXACT:    Literal string replacement.
  Stage 2 - FLEXIBLE: Whitespace-normalized line-by-line matching.
  Stage 3 - REGEX:    Token-based flexible regex with \\s* between tokens.
  Stage 4 - FUZZY:    Levenshtein-distance matching via difflib.SequenceMatcher.
"""

from __future__ import annotations

import difflib
import json
import re
from pathlib import Path
from typing import Optional

from agent_framework.tools.builtin.filesystem import _ensure_within_sandbox
from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FUZZY_MATCH_THRESHOLD = 0.1
"""Maximum Levenshtein distance ratio (10%) for fuzzy matching."""

WHITESPACE_PENALTY_FACTOR = 0.1
"""Penalty multiplier applied to whitespace-only differences in fuzzy scoring."""

_FUZZY_MIN_OLD_STRING_LENGTH = 10
"""Skip fuzzy matching when old_string is shorter than this."""

_FUZZY_COMPLEXITY_LIMIT = 400_000_000
"""Skip fuzzy when source_lines * old_string_length^2 exceeds this."""

_REGEX_DELIMITER_PATTERN = re.compile(r"(\s+|[^\w\s]+)")
"""Splits the search string into tokens at whitespace / punctuation boundaries."""


# ---------------------------------------------------------------------------
# Internal helpers — each stage returns (new_content, count) or None
# ---------------------------------------------------------------------------


def _stage_exact(content: str, old_string: str, new_string: str, replace_all: bool) -> tuple[str, int] | None:
    """Stage 1: literal string replacement."""
    count = content.count(old_string)
    if count == 0:
        return None
    if not replace_all and count > 1:
        raise ValueError(
            f"old_string appears {count} times (EXACT match). "
            "Provide more surrounding context to make it unique, "
            "or set replace_all=True to replace every occurrence."
        )
    new_content = content.replace(old_string, new_string)
    return new_content, count if replace_all else 1


def _stage_flexible(content: str, old_string: str, new_string: str, replace_all: bool) -> tuple[str, int] | None:
    """Stage 2: whitespace-normalized line matching.

    Strips leading/trailing whitespace from each line in both old_string and the
    source file, then locates contiguous runs of trimmed lines that match. The
    *original* lines in the file (preserving indentation) are replaced.
    """
    old_lines = old_string.splitlines()
    old_trimmed = [line.strip() for line in old_lines]

    # Degenerate case: single empty line cannot be meaningfully flexible-matched
    if all(t == "" for t in old_trimmed):
        return None

    source_lines = content.splitlines(keepends=True)
    source_trimmed = [line.strip() for line in source_lines]

    match_count = len(old_trimmed)
    matches: list[tuple[int, int]] = []  # (start_index, end_index_exclusive)

    i = 0
    while i <= len(source_trimmed) - match_count:
        if source_trimmed[i : i + match_count] == old_trimmed:
            matches.append((i, i + match_count))
            i += match_count  # skip past this match
        else:
            i += 1

    if not matches:
        return None

    if not replace_all and len(matches) > 1:
        raise ValueError(
            f"old_string appears {len(matches)} times (FLEXIBLE match). "
            "Provide more surrounding context to make it unique, "
            "or set replace_all=True to replace every occurrence."
        )

    targets = matches if replace_all else matches[:1]

    # Build the replacement text, preserving the trailing newline style of the
    # last replaced line for each match region.
    new_lines_raw = new_string.splitlines(keepends=True)
    # Ensure trailing newline if the original region ended with one
    result_lines: list[str] = []
    prev_end = 0
    for start, end in targets:
        result_lines.extend(source_lines[prev_end:start])
        result_lines.extend(new_lines_raw)
        # If replacement doesn't end with newline but original region did, add one
        if new_lines_raw and not new_lines_raw[-1].endswith("\n") and end < len(source_lines):
            result_lines.append("\n")
        prev_end = end
    result_lines.extend(source_lines[prev_end:])

    new_content = "".join(result_lines)
    return new_content, len(targets)


def _build_flexible_regex(old_string: str) -> re.Pattern[str]:
    """Stage 3 helper: tokenize old_string and build a regex with ``\\s*`` between tokens."""
    tokens = _REGEX_DELIMITER_PATTERN.split(old_string)
    # Keep only non-empty tokens; escape each for regex safety
    meaningful = [re.escape(t) for t in tokens if t]
    if not meaningful:
        raise ValueError("old_string produced no meaningful tokens for regex matching")
    pattern_str = r"\s*".join(meaningful)
    return re.compile(pattern_str, re.DOTALL)


def _stage_regex(content: str, old_string: str, new_string: str, replace_all: bool) -> tuple[str, int] | None:
    """Stage 3: token-based flexible regex matching."""
    try:
        pattern = _build_flexible_regex(old_string)
    except ValueError:
        return None

    matches = list(pattern.finditer(content))
    if not matches:
        return None

    if not replace_all and len(matches) > 1:
        raise ValueError(
            f"old_string appears {len(matches)} times (REGEX match). "
            "Provide more surrounding context to make it unique, "
            "or set replace_all=True to replace every occurrence."
        )

    targets = matches if replace_all else matches[:1]

    # Rebuild content by slicing around each match
    parts: list[str] = []
    prev_end = 0
    for m in targets:
        parts.append(content[prev_end : m.start()])
        parts.append(new_string)
        prev_end = m.end()
    parts.append(content[prev_end:])

    new_content = "".join(parts)
    return new_content, len(targets)


def _levenshtein_ratio(a: str, b: str) -> float:
    """Compute similarity ratio using difflib.SequenceMatcher (0.0–1.0, 1.0 = identical)."""
    return difflib.SequenceMatcher(None, a, b).ratio()


def _whitespace_adjusted_distance(candidate: str, target: str) -> float:
    """Return an adjusted distance ratio that penalises whitespace-only diffs less.

    Returns a value in [0.0, 1.0] where 0.0 means identical.
    """
    raw_ratio = 1.0 - _levenshtein_ratio(candidate, target)

    # Compute what fraction of the difference is purely whitespace
    stripped_candidate = re.sub(r"\s+", "", candidate)
    stripped_target = re.sub(r"\s+", "", target)
    content_distance = 1.0 - _levenshtein_ratio(stripped_candidate, stripped_target)

    whitespace_distance = max(0.0, raw_ratio - content_distance)
    adjusted = content_distance + whitespace_distance * WHITESPACE_PENALTY_FACTOR

    return adjusted


def _stage_fuzzy(
    content: str,
    old_string: str,
    new_string: str,
    replace_all: bool,
) -> tuple[str, int] | None:
    """Stage 4: fuzzy Levenshtein-distance matching with sliding window."""
    old_len = len(old_string)

    # Guard: skip fuzzy for very short search strings
    if old_len < _FUZZY_MIN_OLD_STRING_LENGTH:
        return None

    source_lines = content.splitlines(keepends=True)
    num_source_lines = len(source_lines)

    # Guard: skip fuzzy when complexity is too high
    if num_source_lines * (old_len ** 2) > _FUZZY_COMPLEXITY_LIMIT:
        return None

    old_line_count = old_string.count("\n") + 1
    # Sliding window sizes to try: exact line count, +/- 1 line
    window_sizes = sorted({
        max(1, old_line_count - 1),
        old_line_count,
        old_line_count + 1,
    })

    best_distance = float("inf")
    best_matches: list[tuple[int, int, float]] = []  # (start, end, distance)

    for window in window_sizes:
        if window > num_source_lines:
            continue
        for start in range(num_source_lines - window + 1):
            end = start + window
            candidate = "".join(source_lines[start:end])
            dist = _whitespace_adjusted_distance(candidate, old_string)
            if dist < FUZZY_MATCH_THRESHOLD:
                if dist < best_distance:
                    best_distance = dist
                    best_matches = [(start, end, dist)]
                elif dist == best_distance:
                    # Only add if non-overlapping with existing best matches
                    overlaps = any(
                        not (end <= ms or start >= me)
                        for ms, me, _ in best_matches
                    )
                    if not overlaps:
                        best_matches.append((start, end, dist))

    if not best_matches:
        return None

    if not replace_all and len(best_matches) > 1:
        raise ValueError(
            f"old_string has {len(best_matches)} fuzzy matches (FUZZY, distance={best_distance:.4f}). "
            "Provide more surrounding context to make it unique, "
            "or set replace_all=True to replace every occurrence."
        )

    targets = sorted(best_matches, key=lambda t: t[0])
    if not replace_all:
        targets = targets[:1]

    new_lines_raw = new_string.splitlines(keepends=True)
    result_lines: list[str] = []
    prev_end = 0
    for start, end, _ in targets:
        result_lines.extend(source_lines[prev_end:start])
        result_lines.extend(new_lines_raw)
        if new_lines_raw and not new_lines_raw[-1].endswith("\n") and end < num_source_lines:
            result_lines.append("\n")
        prev_end = end
    result_lines.extend(source_lines[prev_end:])

    new_content = "".join(result_lines)
    return new_content, len(targets)


# ---------------------------------------------------------------------------
# Diff preview helper
# ---------------------------------------------------------------------------

def _unified_diff_preview(old_content: str, new_content: str, file_path: str, context_lines: int = 3) -> str:
    """Generate a unified diff string between old and new content."""
    old_lines = old_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        n=context_lines,
    )
    return "".join(diff)


# ---------------------------------------------------------------------------
# Stage name mapping
# ---------------------------------------------------------------------------

_STAGE_NAMES = {
    1: "EXACT",
    2: "FLEXIBLE",
    3: "REGEX",
    4: "FUZZY",
}


# ---------------------------------------------------------------------------
# Public tool: edit_file
# ---------------------------------------------------------------------------


@tool(
    name="edit_file",
    description=(
        "Edit a file using 4-stage cascade matching: "
        "EXACT literal match, FLEXIBLE whitespace-normalized match, "
        "REGEX token-based match, and FUZZY Levenshtein-distance match. "
        "Finds old_string and replaces it with new_string. "
        "By default old_string must be unique in the file; "
        "set replace_all=True to replace every occurrence. "
        "Optionally provide an 'instruction' describing the intended edit "
        "for LLM-guided editing context."
    ),
    category="filesystem",
    require_confirm=True,
    tags=["system", "file", "write"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="edit modify file replace text",
    activity_description="Editing file",
    prompt=(
        "Edit a file by replacing an exact string match. The old_string must match "
        "exactly including whitespace and indentation."
    ),
    tool_use_summary_tpl="Edited {file_path}",
)
def edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
    instruction: Optional[str] = None,
) -> str:
    """Edit a file by finding old_string via 4-stage cascade matching and replacing it.

    Matching stages tried in order:
      1. EXACT — literal string match (fastest, most precise).
      2. FLEXIBLE — whitespace-stripped line-by-line comparison.
      3. REGEX — tokenize at delimiters, join with ``\\s*``.
      4. FUZZY — sliding-window Levenshtein distance (threshold 10%).

    Args:
        file_path: Absolute path to the file to modify.
        old_string: The text to find (matched via cascade).
        new_string: The replacement text (must differ from old_string).
        replace_all: If True, replace all occurrences. If False (default),
                     old_string must resolve to exactly one match.
        instruction: Optional natural-language description of the intended edit.
                     Stored for LLM-guided editing context; does not affect matching.

    Returns:
        Confirmation message including match strategy and unified diff preview.
    """
    if old_string == new_string:
        raise ValueError("old_string and new_string are identical — nothing to change")

    path = _ensure_within_sandbox(Path(file_path))
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if not path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    content = path.read_text(encoding="utf-8")

    # Cascade through 4 stages
    stages: list[tuple[int, type[object]]] = [
        (1, _stage_exact),     # type: ignore[arg-type]
        (2, _stage_flexible),  # type: ignore[arg-type]
        (3, _stage_regex),     # type: ignore[arg-type]
        (4, _stage_fuzzy),     # type: ignore[arg-type]
    ]

    for stage_num, stage_fn in stages:
        result = stage_fn(content, old_string, new_string, replace_all)  # type: ignore[operator]
        if result is not None:
            new_content, replaced_count = result
            strategy = _STAGE_NAMES[stage_num]
            diff_preview = _unified_diff_preview(content, new_content, file_path)
            path.write_text(new_content, encoding="utf-8")

            message_parts = [
                f"Replaced {replaced_count} occurrence(s) in {file_path}",
                f"strategy: {strategy}",
            ]
            if instruction:
                message_parts.append(f"instruction: {instruction}")
            message_parts.append(f"diff:\n{diff_preview}")
            return "\n".join(message_parts)

    raise ValueError(
        f"old_string not found in {file_path} after all 4 matching stages "
        "(EXACT, FLEXIBLE, REGEX, FUZZY). "
        "Verify the text content and consider providing a larger or more accurate snippet."
    )


# ---------------------------------------------------------------------------
# Public tool: notebook_edit (unchanged)
# ---------------------------------------------------------------------------


@tool(
    name="notebook_edit",
    description=(
        "Edit a Jupyter Notebook (.ipynb) cell by index. "
        "Can replace cell source, change cell type, or insert/delete cells."
    ),
    category="filesystem",
    require_confirm=True,
    tags=["system", "file", "write", "notebook"],
    namespace=SYSTEM_NAMESPACE,
)
def notebook_edit(
    file_path: str,
    cell_index: int,
    new_source: str | None = None,
    cell_type: str | None = None,
    action: str = "replace",
) -> str:
    """Edit a Jupyter Notebook cell.

    Args:
        file_path: Path to the .ipynb file.
        cell_index: Zero-based index of the cell to edit.
        new_source: New source content for the cell. Required for
                    'replace' and 'insert' actions.
        cell_type: Cell type ('code', 'markdown', 'raw'). Only used
                   when inserting or changing type.
        action: One of 'replace', 'insert_before', 'insert_after', 'delete'.

    Returns:
        Confirmation message.
    """
    valid_actions = ("replace", "insert_before", "insert_after", "delete")
    if action not in valid_actions:
        raise ValueError(f"action must be one of {valid_actions}, got '{action}'")

    path = _ensure_within_sandbox(Path(file_path))
    if not path.exists():
        raise FileNotFoundError(f"Notebook not found: {file_path}")

    nb = json.loads(path.read_text(encoding="utf-8"))
    cells = nb.get("cells", [])

    if cell_index < 0 or cell_index >= len(cells):
        if action not in ("insert_before", "insert_after") or cell_index > len(cells):
            raise IndexError(
                f"cell_index {cell_index} out of range (notebook has {len(cells)} cells)"
            )

    def _make_cell(source: str, ctype: str) -> dict:
        cell = {
            "cell_type": ctype,
            "metadata": {},
            "source": source.splitlines(keepends=True),
        }
        if ctype == "code":
            cell["execution_count"] = None
            cell["outputs"] = []
        return cell

    resolved_type = cell_type or "code"

    if action == "delete":
        removed = cells.pop(cell_index)
        msg = f"Deleted cell {cell_index} (was {removed.get('cell_type', '?')})"

    elif action == "replace":
        if new_source is None:
            raise ValueError("new_source is required for 'replace' action")
        target = cells[cell_index]
        target["source"] = new_source.splitlines(keepends=True)
        if cell_type:
            target["cell_type"] = cell_type
            if cell_type == "code" and "outputs" not in target:
                target["outputs"] = []
                target["execution_count"] = None
        msg = f"Replaced cell {cell_index}"

    elif action == "insert_before":
        if new_source is None:
            raise ValueError("new_source is required for 'insert_before' action")
        cells.insert(cell_index, _make_cell(new_source, resolved_type))
        msg = f"Inserted {resolved_type} cell before index {cell_index}"

    elif action == "insert_after":
        if new_source is None:
            raise ValueError("new_source is required for 'insert_after' action")
        cells.insert(cell_index + 1, _make_cell(new_source, resolved_type))
        msg = f"Inserted {resolved_type} cell after index {cell_index}"

    nb["cells"] = cells
    path.write_text(json.dumps(nb, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return msg

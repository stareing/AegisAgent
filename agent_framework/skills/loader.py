"""SKILL.md parser and filesystem discovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_framework.infra.frontmatter import (
    FRONTMATTER_RE,
    mini_yaml_parse,
    parse_frontmatter_file,
)
from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# Re-export for backward compatibility
_FRONTMATTER_RE = FRONTMATTER_RE
_mini_yaml_parse = mini_yaml_parse

# Mapping from frontmatter keys to parsed dict keys for v4.0 fields.
# Keeps the mapping in one place — callers (skill_router) map these
# to Skill model fields.
_V4_FRONTMATTER_MAP: dict[str, str] = {
    "execution-mode": "execution_mode",
    "effort": "effort_level",
    "hooks": "hooks",
    "paths": "paths",
    "arguments": "arguments",
    "context": "context",
    "agent": "agent_ref",
    "shell": "shell",
    "version": "version",
}


def _enrich_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
    """Extract v4.0 frontmatter fields into top-level parsed keys."""
    fm = parsed["frontmatter"]
    for fm_key, parsed_key in _V4_FRONTMATTER_MAP.items():
        if fm_key in fm:
            parsed[parsed_key] = fm[fm_key]
    return parsed


def parse_skill_md(path: Path) -> dict[str, Any] | None:
    """Parse a SKILL.md file into frontmatter dict + body string.

    Returns {"frontmatter": dict, "body": str, "path": Path} or None on failure.
    Enriches result with v4.0 frontmatter fields when present.
    """
    result = parse_frontmatter_file(path)
    if result is None:
        logger.warning("skill.parse_failed", path=str(path))
        return None
    return _enrich_parsed(result)


def _scan_directory(base_dir: Path) -> list[dict[str, Any]]:
    """Scan a single directory for SKILL.md files (both layouts).

    Returns parsed entries with skill_id set but NO dedup applied.
    """
    entries: list[dict[str, Any]] = []

    if not base_dir.is_dir():
        return entries

    # Pattern 1: skills/<name>/SKILL.md
    for child in sorted(base_dir.iterdir()):
        if child.is_dir():
            skill_file = child / "SKILL.md"
            if skill_file.is_file():
                parsed = parse_skill_md(skill_file)
                if parsed:
                    skill_id = parsed["frontmatter"].get("name", child.name)
                    parsed["skill_id"] = skill_id
                    entries.append(parsed)

    # Pattern 2: skills/<name>.md (flat files, not SKILL.md itself)
    for md_file in sorted(base_dir.glob("*.md")):
        if md_file.name == "SKILL.md":
            parsed = parse_skill_md(md_file)
            if parsed:
                skill_id = parsed["frontmatter"].get("name", base_dir.name)
                parsed["skill_id"] = skill_id
                entries.append(parsed)
            continue
        if md_file.stem.startswith("."):
            continue
        parsed = parse_skill_md(md_file)
        if parsed:
            skill_id = parsed["frontmatter"].get("name", md_file.stem)
            parsed["skill_id"] = skill_id
            entries.append(parsed)

    return entries


def discover_skills_with_priority(
    sources: list[tuple[Path, str, int]],
) -> list[dict[str, Any]]:
    """Scan multiple source directories with priority-based dedup.

    Args:
        sources: List of (directory, source_label, priority) tuples.
                 Higher priority wins when the same skill_id appears
                 in multiple sources. Ties are won by the later entry.

    Dedup strategy:
    - Uses ``Path.resolve()`` on each SKILL.md to detect symlink
      duplicates (same physical file in different directories).
    - For the same skill_id from different physical files, higher
      priority overrides lower priority.

    Returns:
        Parsed skill dicts sorted by skill_id. Each dict includes
        ``source_label`` and ``priority`` fields.
    """
    # skill_id → (parsed_dict, priority)
    best: dict[str, tuple[dict[str, Any], int]] = {}
    # Track resolved paths to skip symlink duplicates within a run
    seen_realpaths: set[str] = set()

    for directory, source_label, priority in sources:
        entries = _scan_directory(directory)
        for parsed in entries:
            realpath = str(Path(parsed["path"]).resolve())
            if realpath in seen_realpaths:
                logger.debug(
                    "skill.symlink_dedup",
                    path=str(parsed["path"]),
                    realpath=realpath,
                )
                continue
            seen_realpaths.add(realpath)

            parsed["source_label"] = source_label
            parsed["priority"] = priority

            skill_id = parsed["skill_id"]
            existing = best.get(skill_id)
            if existing is None or priority >= existing[1]:
                if existing is not None:
                    logger.info(
                        "skill.priority_override",
                        skill_id=skill_id,
                        old_source=existing[0].get("source_label"),
                        old_priority=existing[1],
                        new_source=source_label,
                        new_priority=priority,
                    )
                best[skill_id] = (parsed, priority)

    result = [entry for entry, _ in best.values()]
    result.sort(key=lambda e: e["skill_id"])

    all_dirs = [str(d) for d, _, _ in sources]
    logger.info("skill.discovery_complete", count=len(result), dirs=all_dirs)
    return result


def discover_skills(directories: list[Path]) -> list[dict[str, Any]]:
    """Scan directories for SKILL.md files (backward-compatible).

    Supports two layouts:
      skills/<name>/SKILL.md   (directory per skill)
      skills/<name>.md          (flat file, name from filename)

    Delegates to ``discover_skills_with_priority`` internally.
    All directories receive the same priority (0) and source label
    "legacy", preserving first-come-first-serve dedup semantics.
    """
    sources = [(d, "legacy", 0) for d in directories]
    return discover_skills_with_priority(sources)


def load_skill_body(skill_path: str | Path) -> str:
    """Read the full body of a SKILL.md (lazy load on invocation)."""
    path = Path(skill_path)
    if not path.is_file():
        raise FileNotFoundError(f"Skill file not found: {path}")

    text = path.read_text(encoding="utf-8")
    match = _FRONTMATTER_RE.match(text)
    if match:
        return text[match.end():].strip()
    return text.strip()


def load_supporting_file(skill_dir: str | Path, relative_path: str) -> str:
    """Read a companion file from a skill's directory.

    Used by complex skills that reference supporting files like:
      agents/grader.md, references/schemas.md, scripts/run_eval.py

    Args:
        skill_dir: The skill's root directory (where SKILL.md lives).
        relative_path: Path relative to skill_dir (e.g. "agents/grader.md").

    Returns:
        File contents as string.

    Raises:
        FileNotFoundError: If the file doesn't exist.
        ValueError: If the path escapes the skill directory (path traversal).
    """
    base = Path(skill_dir).resolve()
    target = (base / relative_path).resolve()

    # Prevent path traversal outside skill directory
    if not str(target).startswith(str(base)):
        raise ValueError(
            f"Path traversal blocked: {relative_path} escapes skill directory"
        )

    if not target.is_file():
        raise FileNotFoundError(
            f"Supporting file not found: {relative_path} in {skill_dir}"
        )

    return target.read_text(encoding="utf-8")


def list_skill_files(skill_dir: str | Path) -> list[str]:
    """List all files in a skill directory (for skill introspection).

    Returns relative paths from skill_dir.
    """
    base = Path(skill_dir)
    if not base.is_dir():
        return []
    return sorted(
        str(p.relative_to(base))
        for p in base.rglob("*")
        if p.is_file() and not p.name.startswith(".")
        and "__pycache__" not in str(p)
    )

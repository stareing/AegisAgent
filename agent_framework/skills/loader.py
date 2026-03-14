"""SKILL.md parser and filesystem discovery."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

# Minimal YAML-subset parser — avoids pyyaml dependency.
# Handles: scalars, lists (- item), booleans, nulls.
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _mini_yaml_parse(text: str) -> dict[str, Any]:
    """Parse a tiny YAML subset used in frontmatter."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list[str] | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        # List item under current key
        if stripped.startswith("- ") and current_key is not None:
            if current_list is None:
                current_list = []
                result[current_key] = current_list
            current_list.append(stripped[2:].strip().strip('"').strip("'"))
            continue

        # Key: value
        if ":" in stripped:
            # Flush previous list
            current_list = None
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            current_key = key

            if not val:
                # Could be start of a list or empty
                continue

            # Parse value
            val_stripped = val.strip('"').strip("'")
            if val_stripped.lower() in ("true", "yes"):
                result[key] = True
            elif val_stripped.lower() in ("false", "no"):
                result[key] = False
            elif val_stripped.lower() in ("null", "none", "~"):
                result[key] = None
            else:
                result[key] = val_stripped

    return result


def parse_skill_md(path: Path) -> dict[str, Any] | None:
    """Parse a SKILL.md file into frontmatter dict + body string.

    Returns {"frontmatter": dict, "body": str, "path": Path} or None on failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("skill.parse_failed", path=str(path), error=str(e))
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        # No frontmatter — treat entire file as body, derive name from directory
        return {
            "frontmatter": {},
            "body": text.strip(),
            "path": path,
        }

    raw_front = match.group(1)
    body = text[match.end():].strip()
    frontmatter = _mini_yaml_parse(raw_front)

    return {
        "frontmatter": frontmatter,
        "body": body,
        "path": path,
    }


def discover_skills(directories: list[Path]) -> list[dict[str, Any]]:
    """Scan directories for SKILL.md files.

    Supports two layouts:
      skills/<name>/SKILL.md   (directory per skill)
      skills/<name>.md          (flat file, name from filename)
    """
    found: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for base_dir in directories:
        if not base_dir.is_dir():
            continue

        # Pattern 1: skills/<name>/SKILL.md
        for child in sorted(base_dir.iterdir()):
            if child.is_dir():
                skill_file = child / "SKILL.md"
                if skill_file.is_file():
                    parsed = parse_skill_md(skill_file)
                    if parsed:
                        # Default skill_id from directory name
                        skill_id = parsed["frontmatter"].get("name", child.name)
                        if skill_id not in seen_ids:
                            parsed["skill_id"] = skill_id
                            found.append(parsed)
                            seen_ids.add(skill_id)

        # Pattern 2: skills/<name>.md (flat files, not SKILL.md itself)
        for md_file in sorted(base_dir.glob("*.md")):
            if md_file.name == "SKILL.md":
                # Root SKILL.md — parse as unnamed skill
                parsed = parse_skill_md(md_file)
                if parsed:
                    skill_id = parsed["frontmatter"].get("name", base_dir.name)
                    if skill_id not in seen_ids:
                        parsed["skill_id"] = skill_id
                        found.append(parsed)
                        seen_ids.add(skill_id)
                continue
            if md_file.stem.startswith("."):
                continue
            parsed = parse_skill_md(md_file)
            if parsed:
                skill_id = parsed["frontmatter"].get("name", md_file.stem)
                if skill_id not in seen_ids:
                    parsed["skill_id"] = skill_id
                    found.append(parsed)
                    seen_ids.add(skill_id)

    logger.info("skill.discovery_complete", count=len(found),
                dirs=[str(d) for d in directories])
    return found


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

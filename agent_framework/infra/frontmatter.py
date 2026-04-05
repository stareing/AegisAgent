"""Shared frontmatter parser for markdown-based definitions.

Used by both skill loader (skills/loader.py) and agent definition
loader (agent/definition.py) to parse YAML frontmatter from .md files.

Handles a minimal YAML subset — avoids pyyaml dependency.
Supports: scalars, lists (- item), booleans, nulls.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def mini_yaml_parse(text: str) -> dict[str, Any]:
    """Parse a tiny YAML subset used in frontmatter.

    Supports:
    - key: value (strings, booleans, nulls)
    - key: (followed by list items)
    - "- item" list entries under a key
    - Quoted strings (single or double)
    - Comments (#)
    """
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


def parse_frontmatter_file(path: Path) -> dict[str, Any] | None:
    """Parse a markdown file with optional YAML frontmatter.

    Returns {"frontmatter": dict, "body": str, "path": Path} or None on failure.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None

    match = FRONTMATTER_RE.match(text)
    if not match:
        return {
            "frontmatter": {},
            "body": text.strip(),
            "path": path,
        }

    raw_front = match.group(1)
    body = text[match.end():].strip()
    frontmatter = mini_yaml_parse(raw_front)

    return {
        "frontmatter": frontmatter,
        "body": body,
        "path": path,
    }

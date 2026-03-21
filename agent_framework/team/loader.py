"""TEAM.md parser and filesystem discovery.

Same pattern as skills/loader.py — one file per role, discovered
from .agent-team/ directories, parsed once at startup.

Directory layout (mirrors .skills/):
    .agent-team/
    ├── coder/
    │   └── TEAM.md
    ├── reviewer/
    │   └── TEAM.md
    └── analyst.md          # Flat file variant

TEAM.md format (mirrors SKILL.md exactly):
    ---
    name: coder
    description: Write and fix code.
    allowed-tools:
      - read_file
      - write_file
    ---
    [body — role instructions]

Note: team/mail tools are ALWAYS available to teammates regardless
of allowed-tools. This ensures team communication is never blocked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.skills.loader import (
    _mini_yaml_parse,
    _FRONTMATTER_RE,
)

logger = get_logger(__name__)

# Team communication tools — always available, never filtered
TEAM_TOOLS_ALWAYS_AVAILABLE = frozenset({"team", "mail"})


def parse_team_md(path: Path) -> dict[str, Any] | None:
    """Parse a TEAM.md file. Same contract as parse_skill_md."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("team.parse_failed", path=str(path), error=str(e))
        return None

    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {"frontmatter": {}, "body": text.strip(), "path": path}

    raw_front = match.group(1)
    body = text[match.end():].strip()
    frontmatter = _mini_yaml_parse(raw_front)

    # Ensure team/mail tools are always in allowed-tools
    allowed = frontmatter.get("allowed-tools", [])
    if isinstance(allowed, list):
        for tool_name in TEAM_TOOLS_ALWAYS_AVAILABLE:
            if tool_name not in allowed:
                allowed.append(tool_name)
        frontmatter["allowed-tools"] = allowed

    return {"frontmatter": frontmatter, "body": body, "path": path}


def discover_teams(directories: list[Path]) -> list[dict[str, Any]]:
    """Scan directories for TEAM.md files. One file = one role.

    Same discovery logic as discover_skills():
      .agent-team/<name>/TEAM.md   (directory per role)
      .agent-team/<name>.md        (flat file)
    """
    found: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for base_dir in directories:
        if not base_dir.is_dir():
            continue

        # Pattern 1: .agent-team/<name>/TEAM.md
        for child in sorted(base_dir.iterdir()):
            if child.is_dir():
                team_file = child / "TEAM.md"
                if team_file.is_file():
                    parsed = parse_team_md(team_file)
                    if parsed:
                        role_id = parsed["frontmatter"].get("name", child.name)
                        if role_id in seen_ids:
                            logger.error("team.duplicate_role", role=role_id,
                                         path=str(team_file))
                            raise ValueError(
                                f"Duplicate team role '{role_id}' in {team_file}. "
                                f"Each role must be unique."
                            )
                        parsed["team_id"] = role_id
                        found.append(parsed)
                        seen_ids.add(role_id)

        # Pattern 2: .agent-team/<name>.md
        for md_file in sorted(base_dir.glob("*.md")):
            if md_file.name == "TEAM.md":
                continue
            if md_file.stem.startswith("."):
                continue
            parsed = parse_team_md(md_file)
            if parsed:
                role_id = parsed["frontmatter"].get("name", md_file.stem)
                if role_id in seen_ids:
                    logger.error("team.duplicate_role", role=role_id,
                                 path=str(md_file))
                    raise ValueError(
                        f"Duplicate team role '{role_id}' in {md_file}. "
                        f"Each role must be unique."
                    )
                parsed["team_id"] = role_id
                found.append(parsed)
                seen_ids.add(role_id)

    logger.info("team.discovery_complete", count=len(found),
                dirs=[str(d) for d in directories])
    return found

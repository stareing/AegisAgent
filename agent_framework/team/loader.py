"""TEAM.md parser and filesystem discovery.

Same pattern as skills/loader.py — declarative .md files discovered
from .agent-team/ directories, parsed once at startup.

Directory layout (mirrors .skills/):
    .agent-team/
    ├── code-review/
    │   └── TEAM.md
    ├── research/
    │   └── TEAM.md
    └── solo-task.md          # Flat file variant

TEAM.md format (mirrors SKILL.md):
    ---
    name: code-review
    description: Code review team
    roles:
      - coder
      - reviewer
    ---

    [Body — team protocol instructions for Lead]
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
    return {"frontmatter": frontmatter, "body": body, "path": path}


def discover_teams(directories: list[Path]) -> list[dict[str, Any]]:
    """Scan directories for TEAM.md files.

    Same discovery logic as discover_skills():
      .agent-team/<name>/TEAM.md   (directory per team)
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
                        team_id = parsed["frontmatter"].get("name", child.name)
                        if team_id not in seen_ids:
                            parsed["team_id"] = team_id
                            found.append(parsed)
                            seen_ids.add(team_id)

        # Pattern 2: .agent-team/<name>.md
        for md_file in sorted(base_dir.glob("*.md")):
            if md_file.name == "TEAM.md":
                parsed = parse_team_md(md_file)
                if parsed:
                    team_id = parsed["frontmatter"].get("name", base_dir.name)
                    if team_id not in seen_ids:
                        parsed["team_id"] = team_id
                        found.append(parsed)
                        seen_ids.add(team_id)
                continue
            if md_file.stem.startswith("."):
                continue
            parsed = parse_team_md(md_file)
            if parsed:
                team_id = parsed["frontmatter"].get("name", md_file.stem)
                if team_id not in seen_ids:
                    parsed["team_id"] = team_id
                    found.append(parsed)
                    seen_ids.add(team_id)

    # Validate: roles must be unique within each team
    for team_def in found:
        fm = team_def.get("frontmatter", {})
        roles = fm.get("roles", [])
        if len(roles) != len(set(roles)):
            dupes = [r for r in roles if roles.count(r) > 1]
            logger.error(
                "team.duplicate_roles",
                team_id=team_def.get("team_id"),
                duplicates=list(set(dupes)),
            )
            raise ValueError(
                f"Team '{team_def.get('team_id')}' has duplicate roles: {set(dupes)}. "
                f"Each role must be unique within a team."
            )

    logger.info("team.discovery_complete", count=len(found),
                dirs=[str(d) for d in directories])
    return found

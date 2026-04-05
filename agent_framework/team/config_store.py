"""TeamConfigStore — disk-backed team configuration persistence (AT-001).

Storage layout: {base_dir}/{team_name}/config.json
Default base_dir: ~/.agent/teams/
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.team import TeamConfigData, TeamConfigMember

logger = get_logger(__name__)


class TeamConfigStore:
    """Disk-backed team configuration store.

    Persists team membership and metadata to JSON files.
    Queryable via load/list operations.
    """

    def __init__(self, base_dir: str | Path | None = None) -> None:
        if base_dir is None:
            base_dir = Path.home() / ".agent" / "teams"
        self._base_dir = Path(base_dir)

    def save(self, config: TeamConfigData) -> Path:
        """Persist team config to disk. Returns the file path."""
        team_dir = self._base_dir / (config.name or config.team_id)
        team_dir.mkdir(parents=True, exist_ok=True)
        config_path = team_dir / "config.json"

        data = {
            "team_id": config.team_id,
            "lead_id": config.lead_id,
            "name": config.name,
            "members": [
                {"member_id": m.member_id, "role": m.role, "session_id": m.session_id}
                for m in config.members
            ],
            "created_at": config.created_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        config_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("team.config_saved", path=str(config_path), team_id=config.team_id)
        return config_path

    def load(self, team_name: str) -> TeamConfigData | None:
        """Load team config from disk. Returns None if not found."""
        config_path = self._base_dir / team_name / "config.json"
        if not config_path.is_file():
            return None
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            members = [
                TeamConfigMember(
                    member_id=m["member_id"],
                    role=m["role"],
                    session_id=m.get("session_id", ""),
                )
                for m in data.get("members", [])
            ]
            return TeamConfigData(
                team_id=data["team_id"],
                lead_id=data["lead_id"],
                name=data.get("name", team_name),
                members=members,
                created_at=datetime.fromisoformat(data["created_at"]),
                updated_at=datetime.fromisoformat(data["updated_at"]),
            )
        except Exception as exc:
            logger.warning("team.config_load_failed", path=str(config_path), error=str(exc))
            return None

    def delete(self, team_name: str) -> bool:
        """Delete team config from disk. Returns True if deleted."""
        team_dir = self._base_dir / team_name
        config_path = team_dir / "config.json"
        if config_path.is_file():
            config_path.unlink()
            # Remove dir if empty
            try:
                team_dir.rmdir()
            except OSError:
                pass
            logger.info("team.config_deleted", team_name=team_name)
            return True
        return False

    def list_teams(self) -> list[str]:
        """List all persisted team names."""
        if not self._base_dir.is_dir():
            return []
        return sorted(
            d.name for d in self._base_dir.iterdir()
            if d.is_dir() and (d / "config.json").is_file()
        )

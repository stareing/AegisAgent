"""TeamRegistry — thread-safe in-memory registry for team members."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from agent_framework.models.team import (
    TERMINAL_MEMBER_STATUSES,
    TeamMember,
    TeamMemberStatus,
)


class TerminalStatusError(Exception):
    """Raised when attempting to transition out of a terminal status."""

    def __init__(self, agent_id: str, current_status: TeamMemberStatus) -> None:
        self.agent_id = agent_id
        self.current_status = current_status
        super().__init__(
            f"Agent '{agent_id}' is in terminal status {current_status.value} "
            f"and cannot transition to another status."
        )


class TeamRegistry:
    """Thread-safe in-memory registry of team members."""

    def __init__(self, team_id: str | None = None) -> None:
        self._team_id = team_id or uuid.uuid4().hex[:12]
        self._members: dict[str, TeamMember] = {}
        self._lock = threading.Lock()

    def get_team_id(self) -> str:
        return self._team_id

    def register(self, member: TeamMember) -> None:
        with self._lock:
            # Enforce role uniqueness (except "lead" which is always one)
            if member.role != "lead":
                for existing in self._members.values():
                    if existing.role == member.role and existing.agent_id != member.agent_id:
                        raise ValueError(
                            f"Role '{member.role}' already registered to '{existing.agent_id}'. "
                            f"Each role must be unique within a team."
                        )
            self._members[member.agent_id] = member

    def get(self, agent_id: str) -> TeamMember | None:
        with self._lock:
            return self._members.get(agent_id)

    def update_status(self, agent_id: str, status: TeamMemberStatus) -> None:
        with self._lock:
            member = self._members.get(agent_id)
            if member is None:
                raise KeyError(f"Agent '{agent_id}' not found in registry.")
            if member.status in TERMINAL_MEMBER_STATUSES:
                raise TerminalStatusError(agent_id, member.status)
            now = datetime.now(timezone.utc)
            self._members[agent_id] = member.model_copy(
                update={"status": status, "updated_at": now}
            )

    def list_members(
        self, status: TeamMemberStatus | None = None
    ) -> list[TeamMember]:
        with self._lock:
            if status is None:
                return list(self._members.values())
            return [m for m in self._members.values() if m.status == status]

    def remove(self, agent_id: str) -> None:
        with self._lock:
            self._members.pop(agent_id, None)

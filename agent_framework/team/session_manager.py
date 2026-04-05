"""TeamSessionManager — persistent teammate session lifecycle (AT-008).

Tracks session_id per member across multiple run_ids.
Each teammate has at most one active session. The session preserves
conversation history across task assignments and Q&A cycles.

Session lifecycle:
    create_session(member_id) → session_id
    update_session(session_id, run_id, task_id, status)
    get_session(member_id) → TeamSessionState | None
    end_session(session_id)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.team import TeamMemberStatus, TeamSessionState

logger = get_logger(__name__)


class TeamSessionManager:
    """Manages long-lived teammate sessions.

    Each member has at most one active session. Sessions survive across
    multiple runs (task assignments, Q&A continuations).
    """

    def __init__(self, team_id: str) -> None:
        self._team_id = team_id
        # member_id → TeamSessionState
        self._sessions: dict[str, TeamSessionState] = {}

    def create_session(self, member_id: str) -> TeamSessionState:
        """Create a new session for a member. Replaces any existing session."""
        session_id = f"sess_{uuid.uuid4().hex[:12]}"
        session = TeamSessionState(
            session_id=session_id,
            team_id=self._team_id,
            member_id=member_id,
            status=TeamMemberStatus.IDLE,
        )
        self._sessions[member_id] = session
        logger.info("team.session_created", session_id=session_id,
                     member_id=member_id, team_id=self._team_id)
        return session

    def get_session(self, member_id: str) -> TeamSessionState | None:
        """Get the active session for a member."""
        return self._sessions.get(member_id)

    def get_or_create_session(self, member_id: str) -> TeamSessionState:
        """Get existing session or create a new one."""
        session = self._sessions.get(member_id)
        if session is None:
            session = self.create_session(member_id)
        return session

    def update_session(
        self,
        member_id: str,
        run_id: str = "",
        task_id: str = "",
        status: TeamMemberStatus | None = None,
    ) -> TeamSessionState | None:
        """Update an existing session with new run/task info."""
        session = self._sessions.get(member_id)
        if session is None:
            return None
        # TeamSessionState is a regular BaseModel (not frozen), so we can update
        if run_id:
            session.last_run_id = run_id
        if task_id:
            session.current_task_id = task_id
        if status is not None:
            session.status = status
        session.updated_at = datetime.now(timezone.utc)
        return session

    def end_session(self, member_id: str) -> bool:
        """End a session for a member."""
        session = self._sessions.pop(member_id, None)
        if session:
            logger.info("team.session_ended", session_id=session.session_id,
                         member_id=member_id)
            return True
        return False

    def list_sessions(self) -> list[TeamSessionState]:
        """List all active sessions."""
        return list(self._sessions.values())

    def clear(self) -> None:
        """Clear all sessions."""
        self._sessions.clear()

    def has_session(self, member_id: str) -> bool:
        """Check if a member has an active session."""
        return member_id in self._sessions

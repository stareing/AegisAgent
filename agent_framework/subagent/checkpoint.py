"""Checkpoint store for sub-agent state serialization and recovery.

Provides SQLite-backed persistence for AgentState + SessionState snapshots,
enabling true mid-execution resume (CheckpointLevel.STEP_RESUMABLE).

Boundary §8: checkpoint_level declares what recovery level the stored
checkpoint supports. Only STEP_RESUMABLE checkpoints contain full state
sufficient for exact mid-execution resume.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import CheckpointLevel

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentState
    from agent_framework.models.session import SessionState

logger = get_logger(__name__)


class CheckpointData:
    """Deserialized checkpoint data ready for resume."""

    __slots__ = (
        "checkpoint_id", "spawn_id", "agent_state_json", "session_state_json",
        "iteration_index", "checkpoint_level", "summary", "created_at",
    )

    def __init__(
        self,
        checkpoint_id: str,
        spawn_id: str,
        agent_state_json: str,
        session_state_json: str,
        iteration_index: int,
        checkpoint_level: CheckpointLevel,
        summary: str,
        created_at: str,
    ) -> None:
        self.checkpoint_id = checkpoint_id
        self.spawn_id = spawn_id
        self.agent_state_json = agent_state_json
        self.session_state_json = session_state_json
        self.iteration_index = iteration_index
        self.checkpoint_level = checkpoint_level
        self.summary = summary
        self.created_at = created_at

    def restore_agent_state(self) -> AgentState:
        """Deserialize the stored AgentState."""
        from agent_framework.models.agent import AgentState
        return AgentState.model_validate_json(self.agent_state_json)

    def restore_session_state(self) -> SessionState:
        """Deserialize the stored SessionState."""
        from agent_framework.models.session import SessionState
        return SessionState.model_validate_json(self.session_state_json)


_CREATE_CHECKPOINTS_TABLE = """
CREATE TABLE IF NOT EXISTS agent_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    spawn_id TEXT NOT NULL,
    agent_state_json TEXT NOT NULL,
    session_state_json TEXT NOT NULL,
    iteration_index INTEGER NOT NULL DEFAULT 0,
    checkpoint_level TEXT NOT NULL DEFAULT 'COORDINATION_ONLY',
    summary TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

_CREATE_CHECKPOINTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_ckpt_spawn ON agent_checkpoints (spawn_id, created_at DESC);"
)


class SQLiteCheckpointStore:
    """SQLite-backed checkpoint store for sub-agent state persistence.

    Each checkpoint captures a frozen snapshot of AgentState + SessionState
    at a specific iteration boundary, enabling recovery after crash or
    explicit suspend/resume.
    """

    def __init__(self, db_path: str = "data/checkpoints.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_CHECKPOINTS_TABLE)
            self._conn.execute(_CREATE_CHECKPOINTS_INDEX)

    def save(
        self,
        spawn_id: str,
        agent_state: AgentState,
        session_state: SessionState,
        checkpoint_level: CheckpointLevel = CheckpointLevel.STEP_RESUMABLE,
        summary: str = "",
        trigger: str = "user_input",
    ) -> str:
        """Save a checkpoint for a spawn_id. Returns checkpoint_id.

        Args:
            trigger: What triggered this checkpoint. Only "user_input" is
                allowed — checkpoints must represent real user interaction
                boundaries, not synthetic or automated save points.

        Raises:
            ValueError: If trigger is not "user_input".
        """
        if trigger != "user_input":
            raise ValueError(
                f"Checkpoint trigger must be 'user_input', got '{trigger}'. "
                "Checkpoints are only valid at real user interaction boundaries."
            )

        checkpoint_id = f"ckpt_{uuid.uuid4().hex[:12]}"
        agent_json = agent_state.model_dump_json()
        session_json = session_state.model_dump_json()
        iteration_index = agent_state.iteration_count

        with self._conn:
            self._conn.execute(
                """INSERT INTO agent_checkpoints
                   (checkpoint_id, spawn_id, agent_state_json, session_state_json,
                    iteration_index, checkpoint_level, summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    checkpoint_id, spawn_id, agent_json, session_json,
                    iteration_index, checkpoint_level.value, summary,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

        logger.info(
            "checkpoint.saved",
            checkpoint_id=checkpoint_id,
            spawn_id=spawn_id,
            iteration_index=iteration_index,
            level=checkpoint_level.value,
        )
        return checkpoint_id

    def load_latest(self, spawn_id: str) -> CheckpointData | None:
        """Load the most recent checkpoint for a spawn_id."""
        row = self._conn.execute(
            "SELECT * FROM agent_checkpoints WHERE spawn_id = ? ORDER BY created_at DESC LIMIT 1",
            (spawn_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_data(row)

    def load_by_id(self, checkpoint_id: str) -> CheckpointData | None:
        """Load a specific checkpoint by ID."""
        row = self._conn.execute(
            "SELECT * FROM agent_checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_data(row)

    def list_checkpoints(self, spawn_id: str) -> list[dict]:
        """List all checkpoints for a spawn_id (metadata only, no full state)."""
        rows = self._conn.execute(
            "SELECT checkpoint_id, spawn_id, iteration_index, checkpoint_level, summary, created_at "
            "FROM agent_checkpoints WHERE spawn_id = ? ORDER BY created_at DESC",
            (spawn_id,),
        ).fetchall()
        return [
            {
                "checkpoint_id": r["checkpoint_id"],
                "spawn_id": r["spawn_id"],
                "iteration_index": r["iteration_index"],
                "checkpoint_level": r["checkpoint_level"],
                "summary": r["summary"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def delete(self, checkpoint_id: str) -> bool:
        """Delete a specific checkpoint."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM agent_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            )
        return cursor.rowcount > 0

    def delete_for_spawn(self, spawn_id: str) -> int:
        """Delete all checkpoints for a spawn_id."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM agent_checkpoints WHERE spawn_id = ?",
                (spawn_id,),
            )
        return cursor.rowcount

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()

    def _row_to_data(self, row: sqlite3.Row) -> CheckpointData:
        return CheckpointData(
            checkpoint_id=row["checkpoint_id"],
            spawn_id=row["spawn_id"],
            agent_state_json=row["agent_state_json"],
            session_state_json=row["session_state_json"],
            iteration_index=row["iteration_index"],
            checkpoint_level=CheckpointLevel(row["checkpoint_level"]),
            summary=row["summary"],
            created_at=row["created_at"],
        )

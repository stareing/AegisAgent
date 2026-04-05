"""AutoDream — background memory consolidation.

Periodically reviews recent sessions and consolidates patterns into
long-term memory records. Runs as a background task with a gate chain
to prevent excessive processing.

Gate chain (cheapest first):
1. Time gate: minimum hours since last consolidation
2. Session count gate: minimum sessions since last consolidation
3. CAS lock: prevents concurrent consolidation
4. Scan throttle: minimum minutes between scans
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class AutoDreamController:
    """Background memory consolidation controller.

    Examines accumulated session data and extracts recurring patterns,
    insights, and facts into long-term memory.
    """

    def __init__(
        self,
        min_hours_between: float = 24.0,
        min_sessions: int = 5,
        min_scan_interval_minutes: float = 10.0,
        consolidation_callback: Callable[[], Coroutine] | None = None,
        state_file: str | None = None,
    ) -> None:
        self._min_hours = min_hours_between
        self._min_sessions = min_sessions
        self._min_scan_minutes = min_scan_interval_minutes
        self._callback = consolidation_callback
        self._state_file = Path(state_file) if state_file else None
        self._last_consolidation_time: float = 0.0
        self._last_scan_time: float = 0.0
        self._sessions_since_last: int = 0
        self._locked = False
        self._task: asyncio.Task | None = None
        self._running = False
        self._load_state()

    def _load_state(self) -> None:
        """Load persistent state from file."""
        if self._state_file and self._state_file.is_file():
            import json
            try:
                data = json.loads(self._state_file.read_text())
                self._last_consolidation_time = data.get("last_consolidation_time", 0.0)
                self._sessions_since_last = data.get("sessions_since_last", 0)
            except Exception:
                pass

    def _save_state(self) -> None:
        """Persist state to file."""
        if self._state_file:
            import json
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps({
                "last_consolidation_time": self._last_consolidation_time,
                "sessions_since_last": self._sessions_since_last,
            }))

    def record_session_end(self) -> None:
        """Called when an agent session ends. Increments session counter."""
        self._sessions_since_last += 1
        self._save_state()

    def _time_gate(self) -> bool:
        """Check if enough time has passed since last consolidation."""
        if self._last_consolidation_time == 0.0:
            return True  # Never consolidated
        hours_elapsed = (time.time() - self._last_consolidation_time) / 3600.0
        return hours_elapsed >= self._min_hours

    def _session_gate(self) -> bool:
        """Check if enough sessions have accumulated."""
        return self._sessions_since_last >= self._min_sessions

    def _scan_throttle(self) -> bool:
        """Check if enough time has passed since last scan attempt."""
        if self._last_scan_time == 0.0:
            return True
        minutes_elapsed = (time.time() - self._last_scan_time) / 60.0
        return minutes_elapsed >= self._min_scan_minutes

    def _cas_lock(self) -> bool:
        """Attempt to acquire the consolidation lock (CAS)."""
        if self._locked:
            return False
        self._locked = True
        return True

    def _release_lock(self) -> None:
        """Release the consolidation lock."""
        self._locked = False

    async def try_consolidate(self) -> bool:
        """Attempt consolidation if all gates pass.

        Returns True if consolidation was executed.
        """
        self._last_scan_time = time.time()

        # Gate chain: cheapest checks first
        if not self._time_gate():
            logger.debug("auto_dream.time_gate_blocked")
            return False

        if not self._session_gate():
            logger.debug(
                "auto_dream.session_gate_blocked",
                sessions=self._sessions_since_last,
                required=self._min_sessions,
            )
            return False

        if not self._scan_throttle():
            logger.debug("auto_dream.scan_throttle_blocked")
            return False

        if not self._cas_lock():
            logger.debug("auto_dream.lock_blocked")
            return False

        try:
            logger.info(
                "auto_dream.consolidating",
                sessions=self._sessions_since_last,
            )

            if self._callback:
                await self._callback()

            self._last_consolidation_time = time.time()
            self._sessions_since_last = 0
            self._save_state()

            logger.info("auto_dream.consolidation_complete")
            return True
        except Exception as e:
            logger.warning("auto_dream.consolidation_failed", error=str(e))
            return False
        finally:
            self._release_lock()

    async def start_background(self, check_interval_seconds: float = 300.0) -> None:
        """Start background consolidation check loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._bg_loop(check_interval_seconds))
        logger.info("auto_dream.background_started")

    async def stop_background(self) -> None:
        """Stop background consolidation."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("auto_dream.background_stopped")

    async def _bg_loop(self, interval: float) -> None:
        """Background loop."""
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self.try_consolidate()
            except Exception as e:
                logger.warning("auto_dream.bg_check_error", error=str(e))

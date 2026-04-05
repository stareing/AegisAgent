"""SQLite-backed cache for A2A agent discovery results.

Prevents redundant discovery RPCs when delegating to the same agent URL
repeatedly. Each entry has a configurable TTL; expired entries are
transparently skipped on read and can be bulk-purged via cleanup_expired().

SQLite patterns follow agent_framework/memory/sqlite_store.py:
- WAL journal mode for concurrent readers
- busy_timeout for lock contention
- Row factory for dict-like access
- Parent directory auto-creation
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)

DEFAULT_TTL_SECONDS: int = 3600

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS a2a_discovery_cache (
    agent_url TEXT PRIMARY KEY,
    agent_card_json TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    ttl_seconds INTEGER NOT NULL DEFAULT 3600
);
"""

_CREATE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_a2a_cache_discovered "
    "ON a2a_discovery_cache (discovered_at);"
)


class SQLiteA2ADiscoveryCache:
    """SQLite-backed TTL cache for A2A agent card discovery results.

    Thread-safe via SQLite WAL mode + busy_timeout.
    All timestamps are stored as ISO-8601 UTC strings.
    """

    def __init__(self, db_path: str = "data/a2a_discovery_cache.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        with self._conn:
            self._conn.execute(_CREATE_TABLE)
            self._conn.execute(_CREATE_INDEX)

    # ── Public API ────────────────────────────────────────────────

    def get(self, url: str) -> dict | None:
        """Return cached agent card dict if present and not expired, else None."""
        row = self._conn.execute(
            "SELECT agent_card_json, discovered_at, ttl_seconds "
            "FROM a2a_discovery_cache WHERE agent_url = ?",
            (url,),
        ).fetchone()
        if row is None:
            return None

        discovered_at = datetime.fromisoformat(row["discovered_at"])
        ttl_seconds = row["ttl_seconds"]
        elapsed = (datetime.now(timezone.utc) - discovered_at).total_seconds()
        if elapsed > ttl_seconds:
            logger.debug("a2a.discovery_cache.expired", url=url, elapsed=elapsed)
            return None

        logger.debug("a2a.discovery_cache.hit", url=url)
        return json.loads(row["agent_card_json"])

    def put(self, url: str, card_dict: dict, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        """Store or replace a discovery result with the given TTL."""
        now = datetime.now(timezone.utc).isoformat()
        card_json = json.dumps(card_dict, ensure_ascii=False)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO a2a_discovery_cache "
                "(agent_url, agent_card_json, discovered_at, ttl_seconds) "
                "VALUES (?, ?, ?, ?)",
                (url, card_json, now, ttl_seconds),
            )
        logger.debug("a2a.discovery_cache.put", url=url, ttl_seconds=ttl_seconds)

    def invalidate(self, url: str) -> None:
        """Remove a single cached entry."""
        with self._conn:
            self._conn.execute(
                "DELETE FROM a2a_discovery_cache WHERE agent_url = ?",
                (url,),
            )
        logger.debug("a2a.discovery_cache.invalidated", url=url)

    def cleanup_expired(self) -> int:
        """Delete all expired entries. Returns count of rows removed."""
        now = datetime.now(timezone.utc).isoformat()
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM a2a_discovery_cache "
                "WHERE datetime(discovered_at, '+' || ttl_seconds || ' seconds') <= datetime(?)",
                (now,),
            )
        removed = cursor.rowcount
        if removed > 0:
            logger.info("a2a.discovery_cache.cleanup", removed=removed)
        return removed

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()

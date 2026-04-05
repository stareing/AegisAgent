"""Tests for A2A discovery cache (SQLite + TTL).

Covers: cache hit, cache miss, TTL expiry, invalidation, cleanup_expired,
and integration with A2AClientAdapter.
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_framework.protocols.a2a.a2a_discovery_cache import (
    DEFAULT_TTL_SECONDS,
    SQLiteA2ADiscoveryCache,
)


# ── Fixtures ──────────────────────────────────────────────────────

@pytest.fixture
def cache(tmp_path: Path) -> SQLiteA2ADiscoveryCache:
    db_path = str(tmp_path / "test_cache.db")
    c = SQLiteA2ADiscoveryCache(db_path=db_path)
    yield c
    c.close()


SAMPLE_CARD: dict = {
    "url": "http://remote-agent:8080",
    "name": "helper-agent",
    "description": "A helpful remote agent",
    "skills": [
        {"id": "summarize", "name": "Summarize", "description": "Summarize text"},
    ],
}

SAMPLE_URL = "http://remote-agent:8080"


# ── Unit tests: SQLiteA2ADiscoveryCache ───────────────────────────

class TestCacheMiss:
    def test_get_returns_none_for_unknown_url(self, cache: SQLiteA2ADiscoveryCache) -> None:
        assert cache.get("http://unknown:9999") is None


class TestCachePutAndHit:
    def test_put_then_get_returns_card(self, cache: SQLiteA2ADiscoveryCache) -> None:
        cache.put(SAMPLE_URL, SAMPLE_CARD, ttl_seconds=3600)
        result = cache.get(SAMPLE_URL)
        assert result is not None
        assert result["name"] == "helper-agent"
        assert result["url"] == SAMPLE_URL
        assert len(result["skills"]) == 1

    def test_put_replaces_existing(self, cache: SQLiteA2ADiscoveryCache) -> None:
        cache.put(SAMPLE_URL, SAMPLE_CARD, ttl_seconds=3600)
        updated = {**SAMPLE_CARD, "description": "Updated description"}
        cache.put(SAMPLE_URL, updated, ttl_seconds=3600)
        result = cache.get(SAMPLE_URL)
        assert result is not None
        assert result["description"] == "Updated description"


class TestTTLExpiry:
    def test_expired_entry_returns_none(self, cache: SQLiteA2ADiscoveryCache) -> None:
        # Store with 1-second TTL
        cache.put(SAMPLE_URL, SAMPLE_CARD, ttl_seconds=1)
        # Manually backdate the discovered_at to force expiry
        cache._conn.execute(
            "UPDATE a2a_discovery_cache SET discovered_at = ? WHERE agent_url = ?",
            ("2020-01-01T00:00:00+00:00", SAMPLE_URL),
        )
        cache._conn.commit()
        assert cache.get(SAMPLE_URL) is None

    def test_non_expired_entry_returned(self, cache: SQLiteA2ADiscoveryCache) -> None:
        cache.put(SAMPLE_URL, SAMPLE_CARD, ttl_seconds=99999)
        assert cache.get(SAMPLE_URL) is not None


class TestInvalidation:
    def test_invalidate_removes_entry(self, cache: SQLiteA2ADiscoveryCache) -> None:
        cache.put(SAMPLE_URL, SAMPLE_CARD)
        cache.invalidate(SAMPLE_URL)
        assert cache.get(SAMPLE_URL) is None

    def test_invalidate_nonexistent_is_noop(self, cache: SQLiteA2ADiscoveryCache) -> None:
        # Should not raise
        cache.invalidate("http://nonexistent:1234")


class TestCleanupExpired:
    def test_cleanup_removes_only_expired(self, cache: SQLiteA2ADiscoveryCache) -> None:
        # Entry 1: expired (backdated)
        cache.put("http://expired:1", {"name": "expired"}, ttl_seconds=1)
        cache._conn.execute(
            "UPDATE a2a_discovery_cache SET discovered_at = ? WHERE agent_url = ?",
            ("2020-01-01T00:00:00+00:00", "http://expired:1"),
        )
        cache._conn.commit()

        # Entry 2: still valid
        cache.put("http://fresh:2", {"name": "fresh"}, ttl_seconds=99999)

        removed = cache.cleanup_expired()
        assert removed == 1
        assert cache.get("http://expired:1") is None
        assert cache.get("http://fresh:2") is not None

    def test_cleanup_returns_zero_when_nothing_expired(self, cache: SQLiteA2ADiscoveryCache) -> None:
        cache.put(SAMPLE_URL, SAMPLE_CARD, ttl_seconds=99999)
        assert cache.cleanup_expired() == 0


class TestDefaultTTL:
    def test_default_ttl_constant(self) -> None:
        assert DEFAULT_TTL_SECONDS == 3600


# ── Integration: A2AClientAdapter with cache ──────────────────────

class TestAdapterCacheIntegration:
    """Verify discover_agent() uses cache for hits and populates cache on miss."""

    @pytest.mark.asyncio
    async def test_discover_returns_cached_result_without_rpc(self, cache: SQLiteA2ADiscoveryCache) -> None:
        from agent_framework.protocols.a2a.a2a_client_adapter import A2AClientAdapter

        # Pre-populate cache
        cache.put(SAMPLE_URL, SAMPLE_CARD, ttl_seconds=3600)

        adapter = A2AClientAdapter(
            discovery_cache=cache, discovery_cache_ttl_seconds=3600
        )

        # discover_agent should return cached card without importing a2a SDK
        result = await adapter.discover_agent(SAMPLE_URL, alias="helper")
        assert result["name"] == "helper-agent"
        assert "helper" in adapter._known_agents

    @pytest.mark.asyncio
    async def test_discover_populates_cache_on_miss(self, cache: SQLiteA2ADiscoveryCache) -> None:
        from agent_framework.protocols.a2a.a2a_client_adapter import A2AClientAdapter

        adapter = A2AClientAdapter(
            discovery_cache=cache, discovery_cache_ttl_seconds=7200
        )

        # Mock the A2A SDK client
        mock_card = MagicMock()
        mock_card.name = "live-agent"
        mock_card.description = "Discovered live"
        mock_card.skills = []
        mock_card.capabilities = None

        mock_client = MagicMock()
        mock_client.agent_card = mock_card

        url = "http://live-agent:9090"

        # Create a mock module with a mock A2AClient class
        mock_a2a_client_cls = MagicMock()
        mock_a2a_client_cls.get_client_from_agent_card_url = AsyncMock(
            return_value=mock_client
        )
        mock_a2a_client_module = MagicMock()
        mock_a2a_client_module.A2AClient = mock_a2a_client_cls

        with patch.dict("sys.modules", {
            "a2a": MagicMock(),
            "a2a.client": mock_a2a_client_module,
        }):
            result = await adapter.discover_agent(url, alias="live")

        assert result["name"] == "live-agent"
        # Cache should now have the entry
        cached = cache.get(url)
        assert cached is not None
        assert cached["name"] == "live-agent"

    @pytest.mark.asyncio
    async def test_discover_bypasses_expired_cache(self, cache: SQLiteA2ADiscoveryCache) -> None:
        from agent_framework.protocols.a2a.a2a_client_adapter import A2AClientAdapter

        # Pre-populate cache but backdate to expire it
        cache.put(SAMPLE_URL, SAMPLE_CARD, ttl_seconds=1)
        cache._conn.execute(
            "UPDATE a2a_discovery_cache SET discovered_at = ? WHERE agent_url = ?",
            ("2020-01-01T00:00:00+00:00", SAMPLE_URL),
        )
        cache._conn.commit()

        adapter = A2AClientAdapter(
            discovery_cache=cache, discovery_cache_ttl_seconds=3600
        )

        # Mock live discovery
        mock_card = MagicMock()
        mock_card.name = "refreshed-agent"
        mock_card.description = "Refreshed"
        mock_card.skills = []
        mock_card.capabilities = None

        mock_client = MagicMock()
        mock_client.agent_card = mock_card

        with patch.dict("sys.modules", {"a2a": MagicMock(), "a2a.client": MagicMock()}):
            with patch(
                "a2a.client.A2AClient.get_client_from_agent_card_url",
                new_callable=AsyncMock,
                return_value=mock_client,
            ):
                result = await adapter.discover_agent(SAMPLE_URL, alias="refreshed")

        assert result["name"] == "refreshed-agent"
        # Cache should be updated
        cached = cache.get(SAMPLE_URL)
        assert cached is not None
        assert cached["name"] == "refreshed-agent"


class TestAdapterWithoutCache:
    """A2AClientAdapter without cache still works (backward compat)."""

    @pytest.mark.asyncio
    async def test_no_cache_no_error(self) -> None:
        from agent_framework.protocols.a2a.a2a_client_adapter import A2AClientAdapter

        adapter = A2AClientAdapter()  # No cache
        assert adapter._discovery_cache is None


# ── Config integration ────────────────────────────────────────────

class TestA2AConfigCacheTTL:
    def test_default_ttl_in_config(self) -> None:
        from agent_framework.infra.config import A2AConfig
        cfg = A2AConfig()
        assert cfg.discovery_cache_ttl_seconds == 3600

    def test_custom_ttl_in_config(self) -> None:
        from agent_framework.infra.config import A2AConfig
        cfg = A2AConfig(discovery_cache_ttl_seconds=7200)
        assert cfg.discovery_cache_ttl_seconds == 7200

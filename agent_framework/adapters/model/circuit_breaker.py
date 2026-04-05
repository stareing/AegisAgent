"""Circuit breaker with exponential cooldown for model adapters.

Tracks per-adapter failure state and enforces cooldown periods.
Transient failures (rate_limit, overloaded) get probe slots;
permanent failures (auth, billing) skip probing entirely.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from agent_framework.adapters.model.failover_types import (
    PERMANENT_REASONS,
    TRANSIENT_REASONS,
    FailoverReason,
)
from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class CooldownConfig(BaseModel):
    """Cooldown tier configuration (OC-style exponential backoff)."""

    model_config = {"frozen": True}

    # Duration tiers in seconds: attempt 1 -> 60s, 2 -> 300s, 3 -> 1500s, 4+ -> 3600s
    tiers_seconds: list[int] = Field(default_factory=lambda: [60, 300, 1500, 3600])
    probe_transient: bool = True


class _AdapterStats:
    """Mutable per-adapter failure tracking."""

    __slots__ = (
        "error_count",
        "cooldown_until",
        "last_failure_at",
        "last_failure_reason",
        "last_success_at",
        "probe_used",
        "failure_counts",
    )

    def __init__(self) -> None:
        self.error_count: int = 0
        self.cooldown_until: float = 0.0
        self.last_failure_at: float = 0.0
        self.last_failure_reason: FailoverReason | None = None
        self.last_success_at: float = 0.0
        self.probe_used: bool = False
        self.failure_counts: dict[str, int] = {}


class CircuitBreaker:
    """Per-adapter circuit breaker with exponential cooldown.

    States:
    - CLOSED: normal operation, requests pass through
    - OPEN: in cooldown, requests are rejected
    - HALF-OPEN: cooldown expired, one probe request allowed

    Cooldown duration follows OC's exponential schedule:
    error_count 1 -> 60s, 2 -> 300s, 3 -> 1500s, 4+ -> 3600s
    """

    def __init__(self, config: CooldownConfig | None = None) -> None:
        self._config = config or CooldownConfig()
        self._stats: dict[str, _AdapterStats] = {}

    def _get_stats(self, adapter_key: str) -> _AdapterStats:
        if adapter_key not in self._stats:
            self._stats[adapter_key] = _AdapterStats()
        return self._stats[adapter_key]

    def _calculate_cooldown_seconds(self, error_count: int) -> int:
        """Compute cooldown duration from error count (1-indexed)."""
        tiers = self._config.tiers_seconds
        if not tiers:
            return 60
        idx = min(max(error_count - 1, 0), len(tiers) - 1)
        return tiers[idx]

    def record_failure(
        self,
        adapter_key: str,
        reason: FailoverReason,
    ) -> None:
        """Record a failure and set cooldown."""
        stats = self._get_stats(adapter_key)
        now = time.monotonic()

        stats.error_count += 1
        stats.last_failure_at = now
        stats.last_failure_reason = reason
        stats.probe_used = False
        stats.failure_counts[reason.value] = stats.failure_counts.get(reason.value, 0) + 1

        cooldown_secs = self._calculate_cooldown_seconds(stats.error_count)
        stats.cooldown_until = now + cooldown_secs

        logger.info(
            "circuit_breaker.failure_recorded",
            adapter=adapter_key,
            reason=reason.value,
            error_count=stats.error_count,
            cooldown_seconds=cooldown_secs,
        )

    def record_success(self, adapter_key: str) -> None:
        """Record a success -- reset circuit breaker to CLOSED."""
        stats = self._stats.get(adapter_key)
        if stats is None:
            return

        if stats.error_count > 0:
            logger.info(
                "circuit_breaker.recovered",
                adapter=adapter_key,
                previous_errors=stats.error_count,
            )

        stats.error_count = 0
        stats.cooldown_until = 0.0
        stats.last_failure_reason = None
        stats.probe_used = False
        stats.last_success_at = time.monotonic()
        stats.failure_counts.clear()

    def is_in_cooldown(self, adapter_key: str) -> bool:
        """Check if adapter is in cooldown (OPEN state)."""
        stats = self._stats.get(adapter_key)
        if stats is None or stats.cooldown_until <= 0:
            return False
        return time.monotonic() < stats.cooldown_until

    def should_probe(self, adapter_key: str) -> bool:
        """Check if a probe request should be allowed during cooldown.

        Probes are allowed only for transient failures (rate_limit, overloaded)
        and only if the probe slot hasn't been used yet.
        """
        if not self._config.probe_transient:
            return False

        stats = self._stats.get(adapter_key)
        if stats is None:
            return False

        # Only probe if actually in cooldown
        if not self.is_in_cooldown(adapter_key):
            return False

        # Permanent failures skip probing
        if stats.last_failure_reason in PERMANENT_REASONS:
            return False

        # Transient failures get one probe slot
        if stats.last_failure_reason in TRANSIENT_REASONS and not stats.probe_used:
            return True

        return False

    def consume_probe_slot(self, adapter_key: str) -> None:
        """Mark the probe slot as used."""
        stats = self._stats.get(adapter_key)
        if stats:
            stats.probe_used = True

    def clear_expired(self) -> None:
        """Clear expired cooldowns (HALF-OPEN -> CLOSED transition)."""
        now = time.monotonic()
        for key, stats in self._stats.items():
            if stats.cooldown_until > 0 and now >= stats.cooldown_until:
                logger.info(
                    "circuit_breaker.cooldown_expired",
                    adapter=key,
                    error_count=stats.error_count,
                )
                stats.cooldown_until = 0.0
                stats.error_count = 0
                stats.probe_used = False
                stats.failure_counts.clear()

    def get_cooldown_remaining(self, adapter_key: str) -> float:
        """Return seconds remaining in cooldown (0 if not in cooldown)."""
        stats = self._stats.get(adapter_key)
        if stats is None or stats.cooldown_until <= 0:
            return 0.0
        remaining = stats.cooldown_until - time.monotonic()
        return max(0.0, remaining)

    def get_adapter_state(self, adapter_key: str) -> dict:
        """Return adapter state for observability."""
        stats = self._stats.get(adapter_key)
        if stats is None:
            return {"state": "closed", "error_count": 0}

        if self.is_in_cooldown(adapter_key):
            state = "open"
        elif stats.error_count > 0:
            state = "half_open"
        else:
            state = "closed"

        return {
            "state": state,
            "error_count": stats.error_count,
            "cooldown_remaining": self.get_cooldown_remaining(adapter_key),
            "last_failure_reason": stats.last_failure_reason.value if stats.last_failure_reason else None,
            "failure_counts": dict(stats.failure_counts),
        }

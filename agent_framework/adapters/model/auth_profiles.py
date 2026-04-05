"""Auth profile rotation — manages multiple API credentials per provider.

Rotates through available API keys/profiles using least-recently-used
ordering with per-profile cooldown integration. Reduces rate limit
impact by distributing requests across multiple credentials.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class AuthProfile(BaseModel):
    """A single API credential profile."""

    model_config = {"frozen": True}

    profile_id: str
    api_key: str
    api_base: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class _ProfileUsage:
    """Mutable per-profile usage tracking."""

    __slots__ = ("last_used_at", "success_count", "failure_count")

    def __init__(self) -> None:
        self.last_used_at: float = 0.0
        self.success_count: int = 0
        self.failure_count: int = 0


class AuthProfileStore:
    """In-memory store for auth profiles with usage tracking.

    Manages multiple API credentials for a single provider/adapter.
    Works with CircuitBreaker for per-profile cooldown.
    """

    def __init__(self, profiles: list[AuthProfile] | None = None) -> None:
        self._profiles: dict[str, AuthProfile] = {}
        self._usage: dict[str, _ProfileUsage] = {}
        if profiles:
            for p in profiles:
                self.add_profile(p)

    def add_profile(self, profile: AuthProfile) -> None:
        """Register an auth profile."""
        self._profiles[profile.profile_id] = profile
        if profile.profile_id not in self._usage:
            self._usage[profile.profile_id] = _ProfileUsage()

    def remove_profile(self, profile_id: str) -> None:
        """Remove an auth profile."""
        self._profiles.pop(profile_id, None)
        self._usage.pop(profile_id, None)

    def get_profile(self, profile_id: str) -> AuthProfile | None:
        return self._profiles.get(profile_id)

    @property
    def profile_count(self) -> int:
        return len(self._profiles)

    @property
    def profile_ids(self) -> list[str]:
        return list(self._profiles.keys())

    def select_next(
        self,
        is_in_cooldown: Any | None = None,
        should_probe: Any | None = None,
    ) -> AuthProfile | None:
        """Select the next available profile using least-recently-used ordering.

        Args:
            is_in_cooldown: Optional callable(profile_id) -> bool to check circuit breaker.
            should_probe: Optional callable(profile_id) -> bool to check probe eligibility.

        Returns the best available profile, or None if all are in cooldown.
        """
        if not self._profiles:
            return None

        # Sort by last_used_at ascending (least recently used first)
        candidates = sorted(
            self._profiles.keys(),
            key=lambda pid: self._usage[pid].last_used_at,
        )

        # First pass: find non-cooldown profiles
        for pid in candidates:
            if is_in_cooldown and is_in_cooldown(pid):
                continue
            return self._profiles[pid]

        # Second pass: try probe-eligible profiles
        if should_probe:
            for pid in candidates:
                if should_probe(pid):
                    return self._profiles[pid]

        return None

    def mark_used(self, profile_id: str) -> None:
        """Update last_used_at timestamp."""
        usage = self._usage.get(profile_id)
        if usage:
            usage.last_used_at = time.monotonic()

    def mark_success(self, profile_id: str) -> None:
        """Record successful use."""
        usage = self._usage.get(profile_id)
        if usage:
            usage.success_count += 1
            usage.last_used_at = time.monotonic()

    def mark_failure(self, profile_id: str) -> None:
        """Record failed use (actual cooldown managed by CircuitBreaker)."""
        usage = self._usage.get(profile_id)
        if usage:
            usage.failure_count += 1

    def get_stats(self) -> list[dict]:
        """Return usage stats for all profiles (for observability)."""
        return [
            {
                "profile_id": pid,
                "last_used_at": self._usage[pid].last_used_at,
                "success_count": self._usage[pid].success_count,
                "failure_count": self._usage[pid].failure_count,
                "in_store": pid in self._profiles,
            }
            for pid in self._usage
        ]

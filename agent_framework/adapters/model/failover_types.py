"""Failure taxonomy and error classification for model failover.

Maps exceptions to structured failover reasons with severity ordering.
Used by CircuitBreaker to decide cooldown duration and probe eligibility.
"""

from __future__ import annotations

from enum import Enum


class FailoverReason(str, Enum):
    """Classified failure reasons, ordered by severity (highest first)."""

    AUTH_PERMANENT = "auth_permanent"
    BILLING = "billing"
    AUTH = "auth"
    FORMAT = "format"
    MODEL_NOT_FOUND = "model_not_found"
    OVERLOADED = "overloaded"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    UNKNOWN = "unknown"


# Higher severity = longer cooldown, no probe
FAILOVER_SEVERITY: dict[FailoverReason, int] = {
    FailoverReason.AUTH_PERMANENT: 100,
    FailoverReason.BILLING: 90,
    FailoverReason.AUTH: 80,
    FailoverReason.FORMAT: 70,
    FailoverReason.MODEL_NOT_FOUND: 60,
    FailoverReason.OVERLOADED: 30,
    FailoverReason.TIMEOUT: 20,
    FailoverReason.RATE_LIMIT: 10,
    FailoverReason.UNKNOWN: 0,
}

# Transient failures eligible for probe during cooldown
TRANSIENT_REASONS: frozenset[FailoverReason] = frozenset({
    FailoverReason.RATE_LIMIT,
    FailoverReason.OVERLOADED,
    FailoverReason.TIMEOUT,
    FailoverReason.UNKNOWN,
})

# Permanent failures that skip probing
PERMANENT_REASONS: frozenset[FailoverReason] = frozenset({
    FailoverReason.AUTH_PERMANENT,
    FailoverReason.BILLING,
    FailoverReason.AUTH,
    FailoverReason.FORMAT,
    FailoverReason.MODEL_NOT_FOUND,
})


def classify_error(error: Exception) -> FailoverReason:
    """Classify an exception into a FailoverReason.

    Inspects error message and type to determine the most specific reason.
    """
    msg = str(error).lower()
    error_type = type(error).__name__.lower()

    # Authentication errors
    if any(k in msg for k in ("invalid api key", "unauthorized", "403", "invalid_api_key")):
        if "permanent" in msg or "revoked" in msg or "deactivated" in msg:
            return FailoverReason.AUTH_PERMANENT
        return FailoverReason.AUTH

    # Billing errors
    if any(k in msg for k in ("billing", "quota exceeded", "insufficient_quota", "payment")):
        return FailoverReason.BILLING

    # Rate limiting
    if any(k in msg for k in ("rate limit", "rate_limit", "429", "too many requests")):
        return FailoverReason.RATE_LIMIT

    # Model not found
    if any(k in msg for k in ("model not found", "model_not_found", "does not exist", "404")):
        return FailoverReason.MODEL_NOT_FOUND

    # Overloaded
    if any(k in msg for k in ("overloaded", "503", "service unavailable", "capacity")):
        return FailoverReason.OVERLOADED

    # Timeout
    if any(k in msg for k in ("timeout", "timed out")) or "timeout" in error_type:
        return FailoverReason.TIMEOUT

    # Format / schema errors
    if any(k in msg for k in ("invalid request", "400", "bad request", "validation error", "schema")):
        return FailoverReason.FORMAT

    return FailoverReason.UNKNOWN

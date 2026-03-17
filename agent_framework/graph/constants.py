"""Graph constants — sentinels, stream modes, config keys."""

from __future__ import annotations

from enum import Enum


# ── Sentinel node names ────────────────────────────────────────────

START = "__start__"
END = "__end__"


# ── Stream modes ───────────────────────────────────────────────────

class StreamMode(str, Enum):
    """Controls what ``stream()`` yields per step."""

    VALUES = "values"       # Full state snapshot after each node
    UPDATES = "updates"     # Partial dict returned by the node
    DEBUG = "debug"         # Full state + metadata (node name, timing)


# ── Recursion default ──────────────────────────────────────────────

DEFAULT_RECURSION_LIMIT = 25

"""Topic system — hierarchical dot-separated topics with wildcard matching.

Wildcard rules:
- "*"   matches exactly one segment:  "agent.*" matches "agent.progress" but NOT "agent.sp1.progress"
- "**"  matches one or more segments: "agent.**" matches "agent.sp1.progress"
- Exact match: "agent.sp1.progress" only matches itself
"""

from __future__ import annotations

import re
from functools import lru_cache


def topic_matches(pattern: str, topic: str) -> bool:
    """Check if a topic matches a subscription pattern.

    Args:
        pattern: Subscription pattern (may contain * and **).
        topic: Actual topic string to test.

    Returns:
        True if topic matches the pattern.

    Examples:
        >>> topic_matches("agent.*", "agent.progress")
        True
        >>> topic_matches("agent.*", "agent.sp1.progress")
        False
        >>> topic_matches("agent.**", "agent.sp1.progress")
        True
        >>> topic_matches("**", "anything.at.all")
        True
        >>> topic_matches("team.*.shutdown", "team.alpha.shutdown")
        True
    """
    if pattern == "**":
        return True
    if pattern == topic:
        return True
    regex = _compile_pattern(pattern)
    return regex.fullmatch(topic) is not None


@lru_cache(maxsize=256)
def _compile_pattern(pattern: str) -> re.Pattern[str]:
    """Compile a topic pattern to a regex. Cached for performance."""
    parts = pattern.split(".")
    regex_parts: list[str] = []
    for part in parts:
        if part == "**":
            regex_parts.append(r"[^.]+(?:\.[^.]+)*")
        elif part == "*":
            regex_parts.append(r"[^.]+")
        else:
            regex_parts.append(re.escape(part))
    return re.compile(r"\.".join(regex_parts))

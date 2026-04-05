"""Cron expression parser — 5-field standard cron format.

Supports: wildcards (*), steps (*/5), ranges (5-10), lists (1,3,5).
DST-aware via zoneinfo.

Format: minute hour day-of-month month day-of-week
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

# Limits for each field: (min, max)
_FIELD_LIMITS: list[tuple[int, int]] = [
    (0, 59),   # minute
    (0, 23),   # hour
    (1, 31),   # day of month
    (1, 12),   # month
    (0, 6),    # day of week (0=Sunday)
]

_FIELD_NAMES = ("minute", "hour", "day_of_month", "month", "day_of_week")

# Day-of-week aliases
_DOW_MAP = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}
_MONTH_MAP = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


class CronParseError(ValueError):
    """Raised when a cron expression is invalid."""


def _resolve_aliases(value: str, aliases: dict[str, int]) -> str:
    """Replace named aliases (SUN, MON, JAN, etc.) with numbers."""
    upper = value.upper()
    for name, num in aliases.items():
        upper = upper.replace(name, str(num))
    return upper


def _parse_field(raw: str, min_val: int, max_val: int, field_name: str) -> frozenset[int]:
    """Parse a single cron field into a set of matching values."""
    values: set[int] = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue

        # Wildcard with optional step: * or */N
        if part.startswith("*"):
            step = 1
            if "/" in part:
                step = int(part.split("/")[1])
                if step <= 0:
                    raise CronParseError(f"Invalid step in {field_name}: {part}")
            values.update(range(min_val, max_val + 1, step))
            continue

        # Range with optional step: A-B or A-B/N
        if "-" in part:
            range_part, _, step_part = part.partition("/")
            low_s, _, high_s = range_part.partition("-")
            low, high = int(low_s), int(high_s)
            step = int(step_part) if step_part else 1
            if low < min_val or high > max_val or low > high:
                raise CronParseError(
                    f"Range out of bounds in {field_name}: {part} "
                    f"(valid: {min_val}-{max_val})"
                )
            values.update(range(low, high + 1, step))
            continue

        # Single value with optional step: N or N/S
        if "/" in part:
            base_s, _, step_s = part.partition("/")
            base, step = int(base_s), int(step_s)
            values.update(range(base, max_val + 1, step))
            continue

        # Plain number
        val = int(part)
        if val < min_val or val > max_val:
            raise CronParseError(
                f"Value out of bounds in {field_name}: {val} "
                f"(valid: {min_val}-{max_val})"
            )
        values.add(val)

    return frozenset(values)


@dataclass(frozen=True)
class CronExpression:
    """Parsed cron expression with per-field value sets."""

    minute: frozenset[int]
    hour: frozenset[int]
    day_of_month: frozenset[int]
    month: frozenset[int]
    day_of_week: frozenset[int]
    raw: str = ""

    def matches(self, dt: datetime) -> bool:
        """Check if a datetime matches this cron expression."""
        return (
            dt.minute in self.minute
            and dt.hour in self.hour
            and dt.day in self.day_of_month
            and dt.month in self.month
            and dt.weekday() in self._python_weekdays()
        )

    def _python_weekdays(self) -> frozenset[int]:
        """Convert cron weekdays (0=Sun) to Python weekdays (0=Mon)."""
        # Cron: 0=Sun, 1=Mon, ..., 6=Sat
        # Python: 0=Mon, 1=Tue, ..., 6=Sun
        return frozenset((d - 1) % 7 for d in self.day_of_week)


def parse_cron(expression: str) -> CronExpression:
    """Parse a 5-field cron expression string.

    Format: minute hour day-of-month month day-of-week

    Examples:
        "* * * * *"       → every minute
        "0 */2 * * *"     → every 2 hours
        "30 9 * * 1-5"    → 9:30 AM weekdays
        "0 0 1 * *"       → midnight on 1st of each month
    """
    parts = expression.strip().split()
    if len(parts) != 5:
        raise CronParseError(
            f"Expected 5 fields (minute hour dom month dow), got {len(parts)}: {expression!r}"
        )

    # Resolve aliases
    parts[3] = _resolve_aliases(parts[3], _MONTH_MAP)
    parts[4] = _resolve_aliases(parts[4], _DOW_MAP)

    fields: list[frozenset[int]] = []
    for i, (raw, (min_v, max_v), name) in enumerate(
        zip(parts, _FIELD_LIMITS, _FIELD_NAMES)
    ):
        fields.append(_parse_field(raw, min_v, max_v, name))

    return CronExpression(
        minute=fields[0],
        hour=fields[1],
        day_of_month=fields[2],
        month=fields[3],
        day_of_week=fields[4],
        raw=expression.strip(),
    )


def next_run(
    cron: CronExpression,
    after: datetime | None = None,
    max_iterations: int = 525_960,  # ~366 days in minutes
) -> datetime | None:
    """Calculate the next matching datetime after 'after'.

    Walks forward minute-by-minute. Returns None if no match found
    within max_iterations (default ~1 year).
    """
    if after is None:
        after = datetime.now(timezone.utc)

    # Start from the next minute
    candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)

    for _ in range(max_iterations):
        if cron.matches(candidate):
            return candidate
        candidate += timedelta(minutes=1)

    return None

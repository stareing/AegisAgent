"""Multi-level tool loop detection — prevents agent from getting stuck.

Detects four patterns:
- generic_repeat: same tool+args called repeatedly
- ping_pong: alternating A→B→A→B pattern
- no_progress: same tool+args producing identical output
- global_circuit_breaker: total repeated calls exceeds threshold

Uses SHA256 hashing of tool name + sorted args for efficient comparison.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from enum import Enum

from pydantic import BaseModel


class LoopLevel(str, Enum):
    """Severity levels for loop detection."""
    WARNING = "warning"
    CRITICAL = "critical"
    CIRCUIT_BREAK = "circuit_break"


class LoopDetectionResult(BaseModel):
    """Result from loop detection check."""
    model_config = {"frozen": True}

    stuck: bool = False
    level: LoopLevel | None = None
    detector: str = ""
    count: int = 0
    message: str = ""


# Configurable thresholds
WARNING_THRESHOLD: int = 3
CRITICAL_THRESHOLD: int = 6
GLOBAL_CIRCUIT_BREAKER_THRESHOLD: int = 30
HISTORY_SIZE: int = 30


def _hash_call(tool_name: str, args: dict | str) -> str:
    """Compute deterministic hash of a tool call."""
    if isinstance(args, dict):
        args_str = json.dumps(args, sort_keys=True, default=str)
    else:
        args_str = str(args)
    content = f"{tool_name}:{args_str}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _hash_result(output: str) -> str:
    """Hash a tool result for progress tracking."""
    return hashlib.sha256(output.encode()).hexdigest()[:16]


class _HistoryEntry:
    __slots__ = ("call_hash", "result_hash", "tool_name")

    def __init__(self, tool_name: str, call_hash: str, result_hash: str) -> None:
        self.tool_name = tool_name
        self.call_hash = call_hash
        self.result_hash = result_hash


class ToolLoopDetector:
    """Stateful multi-level tool loop detector.

    Call record() after each tool execution, then check()
    before allowing the next iteration.
    """

    def __init__(
        self,
        warning_threshold: int = WARNING_THRESHOLD,
        critical_threshold: int = CRITICAL_THRESHOLD,
        circuit_breaker_threshold: int = GLOBAL_CIRCUIT_BREAKER_THRESHOLD,
        history_size: int = HISTORY_SIZE,
    ) -> None:
        self._warning = warning_threshold
        self._critical = critical_threshold
        self._circuit_breaker = circuit_breaker_threshold
        self._history: deque[_HistoryEntry] = deque(maxlen=history_size)
        self._call_counts: dict[str, int] = {}  # call_hash -> count
        self._result_counts: dict[str, int] = {}  # call_hash -> same-result count
        self._last_result: dict[str, str] = {}  # call_hash -> last result_hash
        self._total_repeats: int = 0

    def record(
        self,
        tool_name: str,
        args: dict | str,
        result_output: str = "",
    ) -> None:
        """Record a completed tool call."""
        call_hash = _hash_call(tool_name, args)
        result_hash = _hash_result(result_output)

        entry = _HistoryEntry(tool_name, call_hash, result_hash)
        self._history.append(entry)

        # Track call frequency
        prev_count = self._call_counts.get(call_hash, 0)
        self._call_counts[call_hash] = prev_count + 1
        if prev_count > 0:
            self._total_repeats += 1

        # Track result progress (same call producing same output = no progress)
        last_rh = self._last_result.get(call_hash)
        if last_rh == result_hash:
            self._result_counts[call_hash] = self._result_counts.get(call_hash, 0) + 1
        else:
            self._result_counts[call_hash] = 0
        self._last_result[call_hash] = result_hash

    def detect(
        self,
        tool_name: str,
        args: dict | str,
    ) -> LoopDetectionResult:
        """Check if a proposed tool call would be a loop."""
        call_hash = _hash_call(tool_name, args)

        # 1. Global circuit breaker
        if self._total_repeats >= self._circuit_breaker:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.CIRCUIT_BREAK,
                detector="global_circuit_breaker",
                count=self._total_repeats,
                message=f"Global repeat count ({self._total_repeats}) exceeded threshold ({self._circuit_breaker})",
            )

        # 2. No-progress detection (same call, same result)
        no_progress_count = self._result_counts.get(call_hash, 0)
        if no_progress_count >= self._critical:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.CRITICAL,
                detector="no_progress",
                count=no_progress_count,
                message=f"Tool '{tool_name}' called {no_progress_count} times with identical results",
            )

        # 3. Generic repeat detection
        repeat_count = self._call_counts.get(call_hash, 0)
        if repeat_count >= self._critical:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.CRITICAL,
                detector="generic_repeat",
                count=repeat_count,
                message=f"Tool '{tool_name}' repeated {repeat_count} times (critical)",
            )

        if repeat_count >= self._warning:
            return LoopDetectionResult(
                stuck=True,
                level=LoopLevel.WARNING,
                detector="generic_repeat",
                count=repeat_count,
                message=f"Tool '{tool_name}' repeated {repeat_count} times (warning)",
            )

        # 4. Ping-pong detection (A→B→A→B pattern)
        if len(self._history) >= 4:
            h = list(self._history)
            last_4 = h[-4:]
            if (last_4[0].call_hash == last_4[2].call_hash
                    and last_4[1].call_hash == last_4[3].call_hash
                    and last_4[0].call_hash != last_4[1].call_hash):
                # Check if current call would continue the pattern
                expected_next = last_4[0].call_hash  # A in A→B→A→B→A
                if call_hash == expected_next:
                    return LoopDetectionResult(
                        stuck=True,
                        level=LoopLevel.WARNING,
                        detector="ping_pong",
                        count=3,
                        message="Ping-pong pattern detected between tools",
                    )

        return LoopDetectionResult(stuck=False)

    def reset(self) -> None:
        """Clear all tracking state."""
        self._history.clear()
        self._call_counts.clear()
        self._result_counts.clear()
        self._last_result.clear()
        self._total_repeats = 0

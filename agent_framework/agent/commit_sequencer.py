"""CommitSequencer — ensures serial state commits from concurrent results.

v2.5.2 §25: When multiple tool calls execute concurrently, their results
must be committed to SessionState in a deterministic order. This prevents
state races where two coroutines call session_state.append_message()
simultaneously and produce non-deterministic message ordering.

The CommitSequencer is an asyncio.Lock wrapper that RunStateController
uses to serialize commit operations. It does NOT own state — it only
provides ordering.
"""
from __future__ import annotations

import asyncio


class CommitSequencer:
    """Serializes concurrent state commits.

    Usage:
        async with sequencer.ordered():
            state_ctrl.project_iteration_to_session(session, result)

    Boundary:
    - ONLY provides ordering — does not own state or format messages.
    - Used by RunCoordinator when committing results.
    - NOT needed for single-iteration sequential loops (current default),
      but protects future parallel-iteration patterns.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    def ordered(self) -> asyncio.Lock:
        """Return the lock for use in an async with block."""
        return self._lock


class ToolCommitSequencer:
    """Serializes tool side-effect commits by input_index order (v2.6.4 §43).

    Concurrent tool execution may complete in arbitrary order, but
    observable side effects (session writes, artifact registration,
    audit records) MUST be committed in input_index order to ensure
    deterministic state.

    Responsibilities:
    - Receive concurrent ToolExecutionOutcome objects
    - Sort by input_index (stable, deterministic)
    - Drive artifact registration in order
    - Drive audit record submission in order
    - Output a stable, projectable result sequence

    Prohibited:
    - Committing side effects in completion order
    - Letting tool threads register artifacts directly
    - Letting tool threads write audit records directly
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    async def commit_outcomes(
        self, outcomes: list
    ) -> list:
        """Sort outcomes by input_index and return in stable order.

        This ensures downstream consumers (MessageProjector, audit, artifact
        registration) always see results in the same order as the original
        tool call requests, regardless of execution timing.
        """
        async with self._lock:
            return sorted(outcomes, key=lambda o: o.input_index)

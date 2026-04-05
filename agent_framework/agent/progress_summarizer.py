"""Background agent progress summarizer.

Generates 3-5 word summaries of agent progress every ~30 seconds
via lightweight LLM calls. Used for UI display and monitoring.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from agent_framework.infra.logger import get_logger
from agent_framework.models.message import Message
from agent_framework.models.stream import StreamEvent, StreamEventType

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentState
    from agent_framework.protocols.core import ModelAdapterProtocol

logger = get_logger(__name__)

_SUMMARIZE_SYSTEM_PROMPT = (
    "You are a progress summarizer. Given the agent's recent iteration history, "
    "produce a 3-5 word summary of what the agent is currently doing. "
    "Reply with ONLY the summary, no punctuation or explanation."
)

_SUMMARIZE_USER_TEMPLATE = (
    "Recent iterations ({count} total):\n{history}\n\n"
    "Summarize in 3-5 words what the agent is doing."
)


class ProgressSummarizer:
    """Periodically summarizes agent progress via lightweight LLM calls.

    The summarizer runs as a background asyncio task, polling the agent's
    iteration_history at a configurable interval and producing short
    human-readable summaries suitable for UI display.
    """

    def __init__(
        self,
        model_adapter: ModelAdapterProtocol,
        interval_seconds: float = 30.0,
    ) -> None:
        self._model_adapter = model_adapter
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._last_summary: str | None = None
        self._agent_state: AgentState | None = None
        self._last_iteration_count: int = 0

    async def start(self, agent_state: AgentState) -> None:
        """Start the background summarizer loop.

        Args:
            agent_state: The mutable agent state to monitor and update.
        """
        if self._task is not None:
            logger.warning("progress_summarizer.already_running")
            return
        self._agent_state = agent_state
        self._last_iteration_count = 0
        self._task = asyncio.create_task(self._summarize_loop())
        logger.info("progress_summarizer.started", interval=self._interval_seconds)

    async def stop(self) -> str | None:
        """Cancel the background task and return the last summary.

        Returns:
            The last generated summary, or None if no summary was produced.
        """
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("progress_summarizer.stopped", last_summary=self._last_summary)
        return self._last_summary

    async def _summarize_loop(self) -> None:
        """Internal loop that periodically generates summaries."""
        while True:
            await asyncio.sleep(self._interval_seconds)
            try:
                await self._generate_summary()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("progress_summarizer.error")

    async def _generate_summary(self) -> None:
        """Generate a single summary from current iteration history."""
        if self._agent_state is None:
            return

        current_count = len(self._agent_state.iteration_history)
        if current_count == 0:
            return

        # Skip if no new iterations since last summary
        if current_count == self._last_iteration_count:
            return

        # Build a compact history string from recent iterations
        recent = self._agent_state.iteration_history[-5:]
        history_lines: list[str] = []
        for ir in recent:
            preview = ir.llm_input_preview or ""
            tool_names = [tr.tool_name for tr in ir.tool_results]
            line = f"Iteration {ir.iteration_index}: tools={tool_names}"
            if preview:
                line += f" preview={preview[:80]}"
            history_lines.append(line)

        history_text = "\n".join(history_lines)
        user_content = _SUMMARIZE_USER_TEMPLATE.format(
            count=current_count,
            history=history_text,
        )

        messages = [
            Message(role="system", content=_SUMMARIZE_SYSTEM_PROMPT),
            Message(role="user", content=user_content),
        ]

        response = await self._model_adapter.complete(
            messages=messages,
            tools=None,
            temperature=0.0,
            max_tokens=30,
        )

        new_summary = (response.content or "").strip()
        if not new_summary:
            return

        # Deduplication: skip if identical to previous summary
        if new_summary == self._last_summary:
            logger.debug("progress_summarizer.dedup_skip", summary=new_summary)
            return

        self._last_summary = new_summary
        self._last_iteration_count = current_count
        self._agent_state.progress_summary = new_summary

        logger.info("progress_summarizer.updated", summary=new_summary)

        # Emit stream event for UI consumers
        _event = StreamEvent(
            type=StreamEventType.PROGRESS_SUMMARY,
            data={"summary": new_summary},
        )
        # Event is created for consumers that subscribe via event bus;
        # direct emission is handled by the coordinator if wired.

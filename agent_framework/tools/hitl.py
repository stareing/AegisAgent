"""HITL (Human-in-the-Loop) handlers for sub-agent interaction.

Implements the HITLHandlerProtocol for different interfaces:
- CLIHITLHandler: stdin-based for terminal usage
- CallbackHITLHandler: callback-based for programmatic/API usage
- QueueHITLHandler: async queue-based for server/WebSocket usage

Flow (PRD §9):
    sub-agent → QUESTION/CONFIRMATION event
    → DelegationExecutor.forward_hitl_request()
    → HITLHandler.handle_hitl_request()
    → user responds
    → HITLResponse
    → DelegationExecutor.resume_subagent()
    → sub-agent RESUMING → RUNNING
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Coroutine
from typing import Any

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import (
    DelegationEvent,
    DelegationEventType,
    HITLRequest,
    HITLResponse,
)

logger = get_logger(__name__)


class CLIHITLHandler:
    """Terminal-based HITL handler using stdin/stdout.

    Blocks the event loop waiting for user input (acceptable for CLI usage).
    """

    async def handle_hitl_request(self, request: HITLRequest) -> HITLResponse:
        """Display the request and wait for user input."""
        print(f"\n{'='*60}")
        print(f"[HITL] Sub-agent needs your input (spawn: {request.spawn_id})")
        print(f"Type: {request.request_type}")
        print(f"Title: {request.title}")
        print(f"Message: {request.message}")

        if request.options:
            print("Options:")
            for i, opt in enumerate(request.options, 1):
                default_marker = " (default)" if opt == request.suggested_default else ""
                print(f"  {i}. {opt}{default_marker}")

        if request.request_type == "confirmation":
            print("Confirm? [y/n]", end=" ")
            answer = await asyncio.to_thread(input)
            response_type = "confirm" if answer.strip().lower() in ("y", "yes") else "deny"
            return HITLResponse(
                request_id=request.request_id,
                response_type=response_type,
            )

        if request.options:
            print(f"Select (1-{len(request.options)}):", end=" ")
            answer = await asyncio.to_thread(input)
            try:
                idx = int(answer.strip()) - 1
                selected = request.options[idx] if 0 <= idx < len(request.options) else None
            except (ValueError, IndexError):
                selected = request.suggested_default
            return HITLResponse(
                request_id=request.request_id,
                response_type="answer",
                selected_option=selected,
            )

        print("Your answer:", end=" ")
        answer = await asyncio.to_thread(input)
        return HITLResponse(
            request_id=request.request_id,
            response_type="answer",
            answer=answer.strip(),
        )


class CallbackHITLHandler:
    """Callback-based HITL handler for programmatic usage.

    Accepts an async callback that receives HITLRequest and returns HITLResponse.
    Useful for API servers, test harnesses, and automated approval flows.
    """

    def __init__(
        self,
        callback: Callable[[HITLRequest], Coroutine[Any, Any, HITLResponse]],
    ) -> None:
        self._callback = callback

    async def handle_hitl_request(self, request: HITLRequest) -> HITLResponse:
        return await self._callback(request)


class QueueHITLHandler:
    """Async queue-based HITL handler scoped to parent runs (boundary §6).

    HITLRequest pending queues belong to the parent run's control plane,
    not to individual sub-agents. This handler enforces per-run limits
    and provides run-scoped request tracking.

    Usage:
        handler = QueueHITLHandler(max_pending_per_run=5)
        # In API endpoint:
        request = await handler.pending_requests.get()
        # ... present to user ...
        await handler.submit_response(HITLResponse(...))
    """

    def __init__(
        self,
        timeout_seconds: float = 300.0,
        max_pending_per_run: int = 5,
    ) -> None:
        self._timeout = timeout_seconds
        self._max_pending_per_run = max_pending_per_run
        self.pending_requests: asyncio.Queue[HITLRequest] = asyncio.Queue()
        self._response_futures: dict[str, asyncio.Future[HITLResponse]] = {}
        # Per-run pending count tracking (boundary §6)
        self._run_pending_counts: dict[str, int] = {}

    async def handle_hitl_request(self, request: HITLRequest) -> HITLResponse:
        """Put request in queue and wait for response.

        Enforces max_pending_per_run. Excess requests are denied immediately.
        """
        if not request.request_id:
            request = request.model_copy(
                update={"request_id": f"hitl_{uuid.uuid4().hex[:8]}"}
            )

        # Enforce per-run HITL limit (boundary §6/§12)
        run_id = request.parent_run_id or "_global"
        current_count = self._run_pending_counts.get(run_id, 0)
        if current_count >= self._max_pending_per_run:
            logger.warning(
                "hitl.run_limit_exceeded",
                request_id=request.request_id,
                parent_run_id=run_id,
                current=current_count,
                max=self._max_pending_per_run,
            )
            return HITLResponse(
                request_id=request.request_id,
                response_type="cancel",
            )

        self._run_pending_counts[run_id] = current_count + 1

        loop = asyncio.get_running_loop()
        future: asyncio.Future[HITLResponse] = loop.create_future()
        self._response_futures[request.request_id] = future

        await self.pending_requests.put(request)

        try:
            return await asyncio.wait_for(future, timeout=self._timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "hitl.timeout",
                request_id=request.request_id,
                timeout_s=self._timeout,
            )
            return HITLResponse(
                request_id=request.request_id,
                response_type="cancel",
            )
        finally:
            self._response_futures.pop(request.request_id, None)
            self._run_pending_counts[run_id] = max(
                0, self._run_pending_counts.get(run_id, 1) - 1
            )

    async def submit_response(self, response: HITLResponse) -> bool:
        """Submit a response for a pending HITL request.

        Returns True if the response was delivered, False if no matching request.
        """
        future = self._response_futures.get(response.request_id)
        if future is None or future.done():
            return False
        future.set_result(response)
        return True

    def get_run_pending_count(self, parent_run_id: str) -> int:
        """Return the number of pending HITL requests for a given parent run."""
        return self._run_pending_counts.get(parent_run_id or "_global", 0)

    @property
    def pending_count(self) -> int:
        return self.pending_requests.qsize()


def event_to_hitl_request(event: DelegationEvent) -> HITLRequest | None:
    """Convert a QUESTION or CONFIRMATION_REQUEST event to a HITLRequest.

    Returns None if the event type is not a HITL-triggering event.
    """
    if event.event_type == DelegationEventType.QUESTION:
        return HITLRequest(
            request_id=event.payload.get("question_id", event.event_id),
            spawn_id=event.spawn_id,
            parent_run_id=event.parent_run_id,
            request_type="question",
            title=str(event.payload.get("title", "")),
            message=str(event.payload.get("question", "")),
            options=list(event.payload.get("options", [])),
            suggested_default=event.payload.get("suggested_default"),
        )

    if event.event_type == DelegationEventType.CONFIRMATION_REQUEST:
        return HITLRequest(
            request_id=event.payload.get("request_id", event.event_id),
            spawn_id=event.spawn_id,
            parent_run_id=event.parent_run_id,
            request_type="confirmation",
            title=str(event.payload.get("action_label", "")),
            message=str(event.payload.get("reason", "")),
        )

    return None

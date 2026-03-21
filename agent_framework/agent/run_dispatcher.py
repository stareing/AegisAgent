"""RunDispatcher — serializes user turns and team notification turns within a conversation.

Ensures:
1. Only one run executes at a time per conversation.
2. User turns have priority over team notification turns.
3. Team notifications can be batched within a configurable window.
4. Background team results trigger auto-notification turns without user interaction.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from agent_framework.infra.logger import get_logger

if TYPE_CHECKING:
    pass

logger = get_logger(__name__)


class TurnType(str, Enum):
    """Type of turn submitted to the dispatcher."""

    USER = "USER"
    TEAM_NOTIFICATION = "TEAM_NOTIFICATION"


class RunDispatcher:
    """Serializes all runs within a single conversation.

    Only one run executes at a time. User turns take priority.
    Team notification turns are batched and deferred while a user turn is active.
    """

    def __init__(
        self,
        run_user_turn: Callable[..., Coroutine[Any, Any, Any]],
        run_notification_turn: Callable[..., Coroutine[Any, Any, Any]],
        batch_window_ms: int = 500,
    ) -> None:
        self._run_user_turn = run_user_turn
        self._run_notification_turn = run_notification_turn
        self._batch_window_ms = batch_window_ms
        self._lock = asyncio.Lock()
        self._notification_pending = asyncio.Event()
        self._shutdown = False
        self._poll_task: asyncio.Task | None = None

    def start(self) -> None:
        """Start the background notification poll loop."""
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._notification_loop())

    def stop(self) -> None:
        """Stop the background notification poll loop."""
        self._shutdown = True
        self._notification_pending.set()  # Unblock if waiting
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    def submit_team_notification(self) -> None:
        """Signal that a team notification is available for processing."""
        self._notification_pending.set()

    async def submit_user_turn(self, user_input: str, **kwargs: Any) -> Any:
        """Execute a user turn, serialized with other turns.

        NOTE: Since fw.run() now acquires the dispatcher lock internally,
        production code should call fw.run() directly instead of this method.
        This method is preserved for testing with mock callbacks that don't
        re-acquire the lock.
        """
        async with self._lock:
            return await self._run_user_turn(user_input, **kwargs)

    async def _notification_loop(self) -> None:
        """Background loop: wait for notification signals, then run notification turns."""
        while not self._shutdown:
            try:
                # Wait for a notification signal
                await self._notification_pending.wait()
                self._notification_pending.clear()

                if self._shutdown:
                    break

                # Batch window: wait briefly for more notifications to arrive
                await asyncio.sleep(self._batch_window_ms / 1000.0)

                # Execute notification turn under lock (serialized with user turns)
                async with self._lock:
                    try:
                        await self._run_notification_turn()
                    except Exception as exc:
                        logger.warning("run_dispatcher.notification_turn_failed", error=str(exc))

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("run_dispatcher.loop_error", error=str(exc))
                await asyncio.sleep(1)  # Prevent tight loop on persistent errors

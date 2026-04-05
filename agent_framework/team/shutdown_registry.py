"""ShutdownRegistry — thread-safe in-memory registry for shutdown requests."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from agent_framework.models.team import (
    TERMINAL_SHUTDOWN_STATUSES,
    ShutdownRequest,
    ShutdownStatus,
)


class TerminalShutdownStatusError(Exception):
    """Raised when attempting to change a shutdown request in terminal status."""

    def __init__(self, request_id: str, current_status: ShutdownStatus) -> None:
        self.request_id = request_id
        self.current_status = current_status
        super().__init__(
            f"Shutdown request '{request_id}' is in terminal status "
            f"{current_status.value} and cannot be changed."
        )


class ShutdownRegistry:
    """Thread-safe in-memory registry for shutdown requests."""

    def __init__(self) -> None:
        self._requests: dict[str, ShutdownRequest] = {}
        self._lock = threading.Lock()

    def create(
        self,
        requester: str,
        target: str,
        reason: str = "",
        team_id: str = "",
    ) -> ShutdownRequest:
        request_id = uuid.uuid4().hex[:16]
        req = ShutdownRequest(
            request_id=request_id,
            requester=requester,
            target=target,
            reason=reason,
            team_id=team_id,
        )
        with self._lock:
            self._requests[request_id] = req
        return req

    def acknowledge(self, request_id: str) -> ShutdownRequest:
        return self._transition(request_id, ShutdownStatus.ACKNOWLEDGED)

    def complete(self, request_id: str) -> ShutdownRequest:
        return self._transition(request_id, ShutdownStatus.COMPLETED)

    def reject(self, request_id: str) -> ShutdownRequest:
        return self._transition(request_id, ShutdownStatus.REJECTED)

    def timeout(self, request_id: str) -> ShutdownRequest:
        return self._transition(request_id, ShutdownStatus.TIMEOUT)

    def get(self, request_id: str) -> ShutdownRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def list_pending(self) -> list[ShutdownRequest]:
        with self._lock:
            return [
                r for r in self._requests.values()
                if r.status == ShutdownStatus.PENDING
            ]

    def _transition(
        self, request_id: str, target: ShutdownStatus
    ) -> ShutdownRequest:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise KeyError(f"Shutdown request '{request_id}' not found.")
            if req.status in TERMINAL_SHUTDOWN_STATUSES:
                raise TerminalShutdownStatusError(request_id, req.status)
            now = datetime.now(timezone.utc)
            updated = req.model_copy(
                update={"status": target, "updated_at": now}
            )
            self._requests[request_id] = updated
            return updated

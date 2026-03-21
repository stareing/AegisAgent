"""PlanRegistry — thread-safe in-memory registry for plan approval requests."""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

from agent_framework.models.team import (
    TERMINAL_PLAN_STATUSES,
    PlanRequest,
    PlanStatus,
)


class TerminalPlanStatusError(Exception):
    """Raised when attempting to change a plan that is already in terminal status."""

    def __init__(self, request_id: str, current_status: PlanStatus) -> None:
        self.request_id = request_id
        self.current_status = current_status
        super().__init__(
            f"Plan '{request_id}' is in terminal status {current_status.value} "
            f"and cannot be changed."
        )


class PlanRegistry:
    """Thread-safe in-memory registry for plan requests."""

    def __init__(self) -> None:
        self._plans: dict[str, PlanRequest] = {}
        self._lock = threading.Lock()

    def create(
        self,
        requester: str,
        approver: str,
        plan_text: str,
        title: str = "",
        risk_level: str = "low",
        task_id: str | None = None,
        team_id: str = "",
    ) -> PlanRequest:
        request_id = uuid.uuid4().hex[:16]
        plan = PlanRequest(
            request_id=request_id,
            requester=requester,
            approver=approver,
            plan_text=plan_text,
            title=title,
            risk_level=risk_level,
            task_id=task_id,
            team_id=team_id,
        )
        with self._lock:
            self._plans[request_id] = plan
        return plan

    def approve(self, request_id: str, feedback: str = "") -> PlanRequest:
        return self._transition(request_id, PlanStatus.APPROVED, feedback)

    def reject(self, request_id: str, feedback: str = "") -> PlanRequest:
        return self._transition(request_id, PlanStatus.REJECTED, feedback)

    def get(self, request_id: str) -> PlanRequest | None:
        with self._lock:
            return self._plans.get(request_id)

    def list_pending(self, approver: str | None = None) -> list[PlanRequest]:
        with self._lock:
            pending = [
                p for p in self._plans.values()
                if p.status == PlanStatus.PENDING
            ]
            if approver is not None:
                pending = [p for p in pending if p.approver == approver]
            return pending

    def _transition(
        self, request_id: str, target: PlanStatus, feedback: str
    ) -> PlanRequest:
        with self._lock:
            plan = self._plans.get(request_id)
            if plan is None:
                raise KeyError(f"Plan '{request_id}' not found.")
            if plan.status in TERMINAL_PLAN_STATUSES:
                raise TerminalPlanStatusError(request_id, plan.status)
            now = datetime.now(timezone.utc)
            updated = plan.model_copy(
                update={
                    "status": target,
                    "feedback": feedback,
                    "updated_at": now,
                }
            )
            self._plans[request_id] = updated
            return updated

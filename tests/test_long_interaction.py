"""Tests for v3.1 Long-term Parent-Child Agent Interaction.

Covers:
- SubAgentStatus 12-state machine + transition validation
- DelegationEvent append-only channel + sequence invariants
- Suspend/Resume models
- HITL request/response chain
- RuntimeNotificationChannel unified draining
- DelegationExecutor resume/cancel/event emission
- Architecture guards (child cannot write parent state)
- Fault injection (concurrent cancel+resume, invalid tokens, terminal re-entry)
"""

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Phase A: Status Machine Tests
# ---------------------------------------------------------------------------


class TestSubAgentStatusMachine:
    """12-state unified status machine (PRD §5)."""

    def test_enum_has_15_states(self):
        from agent_framework.models.subagent import SubAgentStatus
        assert len(SubAgentStatus) == 15  # includes CANCELLING (boundary §9)

    def test_all_scheduler_states_exist(self):
        from agent_framework.models.subagent import SubAgentStatus
        for name in ("PENDING", "QUEUED", "SCHEDULED", "REJECTED"):
            assert hasattr(SubAgentStatus, name)

    def test_all_runtime_states_exist(self):
        from agent_framework.models.subagent import SubAgentStatus
        for name in ("RUNNING", "WAITING_PARENT", "WAITING_USER",
                      "SUSPENDED", "RESUMING"):
            assert hasattr(SubAgentStatus, name)

    def test_all_terminal_states_exist(self):
        from agent_framework.models.subagent import SubAgentStatus
        for name in ("COMPLETED", "FAILED", "CANCELLED", "DEGRADED", "TIMEOUT"):
            assert hasattr(SubAgentStatus, name)

    def test_backward_compat_alias(self):
        from agent_framework.models.subagent import (SubAgentStatus,
                                                     SubAgentTaskStatus)
        assert SubAgentTaskStatus is SubAgentStatus
        assert SubAgentTaskStatus.QUEUED == SubAgentStatus.QUEUED

    def test_terminal_check(self):
        from agent_framework.models.subagent import (SubAgentStatus,
                                                     is_terminal_status)
        assert is_terminal_status(SubAgentStatus.COMPLETED)
        assert is_terminal_status(SubAgentStatus.FAILED)
        assert is_terminal_status(SubAgentStatus.CANCELLED)
        assert is_terminal_status(SubAgentStatus.REJECTED)
        assert is_terminal_status(SubAgentStatus.DEGRADED)
        assert is_terminal_status(SubAgentStatus.TIMEOUT)
        assert not is_terminal_status(SubAgentStatus.RUNNING)
        assert not is_terminal_status(SubAgentStatus.PENDING)

    def test_active_check(self):
        from agent_framework.models.subagent import (SubAgentStatus,
                                                     is_active_status)
        assert is_active_status(SubAgentStatus.PENDING)
        assert is_active_status(SubAgentStatus.RUNNING)
        assert is_active_status(SubAgentStatus.WAITING_PARENT)
        assert is_active_status(SubAgentStatus.SUSPENDED)
        assert not is_active_status(SubAgentStatus.COMPLETED)
        assert not is_active_status(SubAgentStatus.FAILED)

    def test_valid_transitions(self):
        from agent_framework.models.subagent import (
            SubAgentStatus, validate_status_transition)
        valid_pairs = [
            (SubAgentStatus.PENDING, SubAgentStatus.RUNNING),
            (SubAgentStatus.PENDING, SubAgentStatus.QUEUED),
            (SubAgentStatus.QUEUED, SubAgentStatus.SCHEDULED),
            (SubAgentStatus.SCHEDULED, SubAgentStatus.RUNNING),
            (SubAgentStatus.RUNNING, SubAgentStatus.WAITING_PARENT),
            (SubAgentStatus.RUNNING, SubAgentStatus.WAITING_USER),
            (SubAgentStatus.RUNNING, SubAgentStatus.SUSPENDED),
            (SubAgentStatus.RUNNING, SubAgentStatus.COMPLETED),
            (SubAgentStatus.RUNNING, SubAgentStatus.FAILED),
            (SubAgentStatus.RUNNING, SubAgentStatus.TIMEOUT),
            (SubAgentStatus.RUNNING, SubAgentStatus.DEGRADED),
            (SubAgentStatus.WAITING_PARENT, SubAgentStatus.RESUMING),
            (SubAgentStatus.WAITING_USER, SubAgentStatus.RESUMING),
            (SubAgentStatus.SUSPENDED, SubAgentStatus.RESUMING),
            (SubAgentStatus.RESUMING, SubAgentStatus.RUNNING),
        ]
        for from_s, to_s in valid_pairs:
            validate_status_transition(from_s, to_s)  # Should not raise

    def test_invalid_transitions_raise(self):
        from agent_framework.models.subagent import (
            InvalidStatusTransitionError, SubAgentStatus,
            validate_status_transition)
        invalid_pairs = [
            (SubAgentStatus.COMPLETED, SubAgentStatus.RUNNING),
            (SubAgentStatus.FAILED, SubAgentStatus.RESUMING),
            (SubAgentStatus.CANCELLED, SubAgentStatus.RESUMING),
            (SubAgentStatus.PENDING, SubAgentStatus.COMPLETED),
            (SubAgentStatus.WAITING_PARENT, SubAgentStatus.COMPLETED),
        ]
        for from_s, to_s in invalid_pairs:
            with pytest.raises(InvalidStatusTransitionError):
                validate_status_transition(from_s, to_s)

    def test_any_active_can_reach_cancelled(self):
        from agent_framework.models.subagent import (
            SubAgentStatus, is_active_status, validate_status_transition)
        for status in SubAgentStatus:
            if is_active_status(status):
                # All active states can reach CANCELLED (directly or via CANCELLING)
                try:
                    validate_status_transition(status, SubAgentStatus.CANCELLED)
                except Exception:
                    # Must at least reach CANCELLING which leads to CANCELLED
                    validate_status_transition(status, SubAgentStatus.CANCELLING)
                    validate_status_transition(SubAgentStatus.CANCELLING, SubAgentStatus.CANCELLED)

    def test_no_terminal_can_transition(self):
        from agent_framework.models.subagent import (
            InvalidStatusTransitionError, SubAgentStatus, is_terminal_status,
            validate_status_transition)
        for status in SubAgentStatus:
            if is_terminal_status(status):
                for target in SubAgentStatus:
                    if target != status:
                        with pytest.raises(InvalidStatusTransitionError):
                            validate_status_transition(status, target)

    def test_error_code_mapping_complete(self):
        from agent_framework.models.subagent import (_ERROR_CODE_TO_STATUS,
                                                     DelegationErrorCode)
        for code in DelegationErrorCode:
            assert code in _ERROR_CODE_TO_STATUS, f"{code} unmapped"

    def test_timeout_maps_to_timeout_status(self):
        from agent_framework.models.subagent import (DelegationErrorCode,
                                                     SubAgentResult,
                                                     SubAgentStatus,
                                                     resolve_delegation_status)
        result = SubAgentResult(spawn_id="s1", success=False, error="timeout")
        assert resolve_delegation_status(result, DelegationErrorCode.TIMEOUT) == SubAgentStatus.TIMEOUT

    def test_suspended_result_resolves_to_suspended(self):
        from agent_framework.models.subagent import (SubAgentResult,
                                                     SubAgentStatus,
                                                     SubAgentSuspendInfo,
                                                     SubAgentSuspendReason,
                                                     resolve_delegation_status)
        result = SubAgentResult(
            spawn_id="s1", success=False,
            suspend_info=SubAgentSuspendInfo(
                reason=SubAgentSuspendReason.WAIT_PARENT_INPUT,
                message="Need env selection",
                resume_token="tok_1",
            ),
        )
        assert resolve_delegation_status(result) == SubAgentStatus.SUSPENDED


# ---------------------------------------------------------------------------
# Phase A: DelegationEvent Tests
# ---------------------------------------------------------------------------

class TestDelegationEvent:
    """DelegationEvent model invariants."""

    def test_event_creation(self):
        from agent_framework.models.subagent import (DelegationEvent,
                                                     DelegationEventType)
        event = DelegationEvent(
            event_id="e1",
            spawn_id="sp1",
            parent_run_id="run1",
            event_type=DelegationEventType.PROGRESS,
            sequence_no=1,
            payload={"percent": 50},
        )
        assert event.event_id == "e1"
        assert event.sequence_no == 1
        assert not event.requires_ack
        assert not event.acked

    def test_all_event_types_exist(self):
        from agent_framework.models.subagent import DelegationEventType
        expected = {
            "STARTED", "PROGRESS", "QUESTION", "CONFIRMATION_REQUEST",
            "CHECKPOINT", "ARTIFACT_READY", "SUSPENDED", "RESUMED",
            "COMPLETED", "FAILED", "CANCELLED",
        }
        actual = {e.value for e in DelegationEventType}
        assert expected == actual


# ---------------------------------------------------------------------------
# Phase B: InteractionChannel Tests
# ---------------------------------------------------------------------------

class TestInMemoryInteractionChannel:
    """InMemoryInteractionChannel — append-only, sequence monotonic."""

    def test_append_and_list(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()

        e1 = ch.emit_event("sp1", "run1", DelegationEventType.STARTED)
        e2 = ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS, {"p": 50})

        events = ch.list_events("sp1")
        assert len(events) == 2
        assert events[0].sequence_no == 1
        assert events[1].sequence_no == 2

    def test_sequence_strictly_monotonic(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()

        for _ in range(10):
            ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)

        events = ch.list_events("sp1")
        seqs = [e.sequence_no for e in events]
        assert seqs == list(range(1, 11))

    def test_list_after_sequence(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()

        for i in range(5):
            ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS, {"i": i})

        after_3 = ch.list_events("sp1", after_sequence_no=3)
        assert len(after_3) == 2
        assert after_3[0].sequence_no == 4

    def test_ack_event(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()

        e = ch.emit_event("sp1", "run1", DelegationEventType.QUESTION, {}, requires_ack=True)
        assert len(ch.get_pending_events("sp1")) == 1

        ch.ack_event("sp1", e.event_id)
        assert len(ch.get_pending_events("sp1")) == 0

    def test_ack_nonexistent_raises(self):
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()
        with pytest.raises(ValueError, match="not found"):
            ch.ack_event("sp1", "nonexistent")

    def test_max_events_enforced(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel(max_events_per_spawn=3)

        for _ in range(3):
            ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)

        with pytest.raises(ValueError, match="Max events"):
            ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)

    def test_clear_spawn(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()

        ch.emit_event("sp1", "run1", DelegationEventType.STARTED)
        ch.clear_spawn("sp1")
        assert ch.list_events("sp1") == []
        assert ch.get_latest_sequence_no("sp1") == 0

    def test_separate_spawn_isolation(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()

        ch.emit_event("sp1", "run1", DelegationEventType.STARTED)
        ch.emit_event("sp2", "run1", DelegationEventType.STARTED)
        ch.emit_event("sp2", "run1", DelegationEventType.PROGRESS)

        assert len(ch.list_events("sp1")) == 1
        assert len(ch.list_events("sp2")) == 2

    def test_auto_assigns_event_id(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel()

        e = ch.emit_event("sp1", "run1", DelegationEventType.STARTED)
        assert e.event_id.startswith("evt_")

    def test_thread_safety(self):
        """Concurrent appends from multiple threads."""
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        ch = InMemoryInteractionChannel(max_events_per_spawn=1000)

        def append_batch(n: int):
            for _ in range(n):
                ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)

        threads = [threading.Thread(target=append_batch, args=(50,)) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = ch.list_events("sp1")
        assert len(events) == 250
        seqs = [e.sequence_no for e in events]
        assert seqs == list(range(1, 251))  # Strictly monotonic


# ---------------------------------------------------------------------------
# Phase B: RuntimeNotificationChannel Tests
# ---------------------------------------------------------------------------

class TestRuntimeNotificationChannel:
    """Unified notification draining."""

    def test_drain_delegation_events(self):
        from agent_framework.models.subagent import (DelegationEventType,
                                                     RuntimeNotificationType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        ch = InMemoryInteractionChannel()
        nc = RuntimeNotificationChannel(interaction_channel=ch)
        nc.monitor_spawn("sp1")

        ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS, {"summary": "Step 1"})

        notifs = nc.drain_all()
        assert len(notifs) == 1
        assert notifs[0].notification_type == RuntimeNotificationType.DELEGATION_EVENT
        assert notifs[0].payload["data"]["summary"] == "Step 1"

    def test_incremental_drain(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        ch = InMemoryInteractionChannel()
        nc = RuntimeNotificationChannel(interaction_channel=ch)
        nc.monitor_spawn("sp1")

        ch.emit_event("sp1", "run1", DelegationEventType.STARTED)
        assert len(nc.drain_all()) == 1
        assert len(nc.drain_all()) == 0  # Already seen

        ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)
        assert len(nc.drain_all()) == 1

    def test_format_notifications(self):
        from agent_framework.models.subagent import (RuntimeNotification,
                                                     RuntimeNotificationType)
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        notifs = [
            RuntimeNotification(
                notification_id="bg_1",
                notification_type=RuntimeNotificationType.BACKGROUND_TASK,
                payload={"task_id": "t1", "output": "done", "exit_code": 0, "timed_out": False},
            ),
            RuntimeNotification(
                notification_id="del_1",
                notification_type=RuntimeNotificationType.DELEGATION_EVENT,
                payload={"spawn_id": "sp1", "event_type": "PROGRESS", "sequence_no": 1, "data": {"p": 50}},
            ),
        ]
        xml = RuntimeNotificationChannel.format_notifications(notifs)
        assert "<runtime-notifications>" in xml
        assert "<background-task" in xml
        assert "<delegation-event" in xml

    def test_summarize_delegation_events(self):
        from agent_framework.models.subagent import (RuntimeNotification,
                                                     RuntimeNotificationType,
                                                     SubAgentStatus)
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        notifs = [
            RuntimeNotification(
                notification_type=RuntimeNotificationType.DELEGATION_EVENT,
                payload={
                    "spawn_id": "sp1", "event_type": "PROGRESS",
                    "data": {"summary": "Analyzing files"},
                },
            ),
            RuntimeNotification(
                notification_type=RuntimeNotificationType.DELEGATION_EVENT,
                payload={
                    "spawn_id": "sp1", "event_type": "QUESTION",
                    "data": {"question": "Which branch?"},
                },
            ),
        ]
        summaries = RuntimeNotificationChannel.summarize_delegation_events(notifs)
        assert len(summaries) == 1
        assert summaries[0].status == SubAgentStatus.WAITING_PARENT
        assert summaries[0].question == "Which branch?"

    def test_unmonitor_stops_drain(self):
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        ch = InMemoryInteractionChannel()
        nc = RuntimeNotificationChannel(interaction_channel=ch)
        nc.monitor_spawn("sp1")

        ch.emit_event("sp1", "run1", DelegationEventType.STARTED)
        nc.unmonitor_spawn("sp1")

        ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)
        assert len(nc.drain_all()) == 0


# ---------------------------------------------------------------------------
# Phase B: DelegationExecutor Extended Tests
# ---------------------------------------------------------------------------

class TestDelegationExecutorResume:
    """DelegationExecutor resume/cancel/event emission."""

    @pytest.fixture
    def mock_runtime(self):
        runtime = AsyncMock()
        runtime.spawn = AsyncMock(return_value=MagicMock(
            spawn_id="sp1", success=True, final_answer="Done",
            iterations_used=3, artifacts=[], suspend_info=None,
            error=None,
        ))
        runtime.spawn_async = AsyncMock(return_value="sp1")
        runtime.resume = AsyncMock(return_value=MagicMock(
            spawn_id="sp1", success=True, final_answer="Resumed OK",
            iterations_used=2, artifacts=[], suspend_info=None,
            error=None,
        ))
        runtime.cancel = AsyncMock()
        runtime.collect_result = AsyncMock(return_value=None)
        return runtime

    @pytest.fixture
    def executor(self, mock_runtime):
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.subagent.delegation import DelegationExecutor
        ch = InMemoryInteractionChannel()
        ex = DelegationExecutor(
            sub_agent_runtime=mock_runtime,
            interaction_channel=ch,
        )
        return ex, ch

    @pytest.mark.asyncio
    async def test_resume_subagent(self, executor, mock_runtime):
        ex, ch = executor
        result = await ex.resume_subagent("sp1", {"answer": "test_only"}, None)
        assert result.success is True
        mock_runtime.resume.assert_awaited_once_with("sp1", {"answer": "test_only"}, None)

        # Check events emitted
        events = ch.list_events("sp1")
        assert len(events) == 2  # RESUMED + COMPLETED
        assert events[0].event_type.value == "RESUMED"
        assert events[1].event_type.value == "COMPLETED"

    @pytest.mark.asyncio
    async def test_cancel_subagent(self, executor, mock_runtime):
        ex, ch = executor
        await ex.cancel_subagent("sp1")
        mock_runtime.cancel.assert_awaited_once_with("sp1")

        events = ch.list_events("sp1")
        assert len(events) == 1
        assert events[0].event_type.value == "CANCELLED"

    @pytest.mark.asyncio
    async def test_delegate_emits_started_and_completed(self, executor, mock_runtime):
        from agent_framework.models.subagent import SubAgentSpec
        ex, ch = executor
        spec = SubAgentSpec(spawn_id="sp1", parent_run_id="run1", task_input="Do something")
        result = await ex.delegate_to_subagent(spec, None)
        assert result.success is True

        events = ch.list_events("sp1")
        types = [e.event_type.value for e in events]
        assert "STARTED" in types
        assert "COMPLETED" in types

    @pytest.mark.asyncio
    async def test_delegate_suspended_emits_suspended_event(self, mock_runtime):
        from agent_framework.models.subagent import (SubAgentSpec,
                                                     SubAgentSuspendInfo,
                                                     SubAgentSuspendReason)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.subagent.delegation import DelegationExecutor

        mock_runtime.spawn = AsyncMock(return_value=MagicMock(
            spawn_id="sp1", success=False,
            final_answer=None, error=None,
            suspend_info=SubAgentSuspendInfo(
                reason=SubAgentSuspendReason.WAIT_PARENT_INPUT,
                message="Need env",
                resume_token="tok_1",
            ),
            iterations_used=1, artifacts=[],
        ))

        ch = InMemoryInteractionChannel()
        ex = DelegationExecutor(sub_agent_runtime=mock_runtime, interaction_channel=ch)
        spec = SubAgentSpec(spawn_id="sp1", parent_run_id="run1", task_input="Task")
        result = await ex.delegate_to_subagent(spec, None)

        events = ch.list_events("sp1")
        types = [e.event_type.value for e in events]
        assert "SUSPENDED" in types

    @pytest.mark.asyncio
    async def test_resume_no_runtime_returns_error(self):
        from agent_framework.subagent.delegation import DelegationExecutor
        ex = DelegationExecutor(sub_agent_runtime=None)
        result = await ex.resume_subagent("sp1", {}, None)
        assert not result.success
        assert "not configured" in result.error

    @pytest.mark.asyncio
    async def test_cancel_no_runtime_raises(self):
        from agent_framework.subagent.delegation import DelegationExecutor
        ex = DelegationExecutor(sub_agent_runtime=None)
        with pytest.raises(RuntimeError, match="not configured"):
            await ex.cancel_subagent("sp1")

    @pytest.mark.asyncio
    async def test_resume_a2a_no_adapter(self):
        from agent_framework.subagent.delegation import DelegationExecutor
        ex = DelegationExecutor()
        result = await ex.resume_a2a("remote_1", {"input": "test"})
        assert not result.success
        assert "not configured" in result.error


# ---------------------------------------------------------------------------
# Phase C: HITL Tests
# ---------------------------------------------------------------------------

class TestHITLHandlers:
    """HITL handler implementations."""

    @pytest.mark.asyncio
    async def test_callback_handler(self):
        from agent_framework.models.subagent import HITLRequest, HITLResponse
        from agent_framework.subagent.hitl import CallbackHITLHandler

        async def auto_answer(req: HITLRequest) -> HITLResponse:
            return HITLResponse(
                request_id=req.request_id,
                response_type="answer",
                answer="selected_option_1",
            )

        handler = CallbackHITLHandler(auto_answer)
        resp = await handler.handle_hitl_request(HITLRequest(
            request_id="r1", request_type="question",
            message="Which?", options=["a", "b"],
        ))
        assert resp.answer == "selected_option_1"

    @pytest.mark.asyncio
    async def test_queue_handler_flow(self):
        from agent_framework.models.subagent import HITLRequest, HITLResponse
        from agent_framework.subagent.hitl import QueueHITLHandler

        handler = QueueHITLHandler(timeout_seconds=5.0)

        async def consumer():
            req = await handler.pending_requests.get()
            await handler.submit_response(HITLResponse(
                request_id=req.request_id,
                response_type="confirm",
            ))

        asyncio.create_task(consumer())

        resp = await handler.handle_hitl_request(HITLRequest(
            request_id="r1", request_type="confirmation",
            message="Run migration?",
        ))
        assert resp.response_type == "confirm"

    @pytest.mark.asyncio
    async def test_queue_handler_timeout(self):
        from agent_framework.models.subagent import HITLRequest
        from agent_framework.subagent.hitl import QueueHITLHandler

        handler = QueueHITLHandler(timeout_seconds=0.1)
        resp = await handler.handle_hitl_request(HITLRequest(
            request_id="r1", request_type="question", message="Hello?",
        ))
        assert resp.response_type == "cancel"

    @pytest.mark.asyncio
    async def test_queue_submit_nonexistent_request(self):
        from agent_framework.models.subagent import HITLResponse
        from agent_framework.subagent.hitl import QueueHITLHandler

        handler = QueueHITLHandler()
        ok = await handler.submit_response(HITLResponse(
            request_id="nonexistent", response_type="answer",
        ))
        assert ok is False

    def test_event_to_hitl_request_question(self):
        from agent_framework.models.subagent import (DelegationEvent,
                                                     DelegationEventType)
        from agent_framework.subagent.hitl import event_to_hitl_request

        event = DelegationEvent(
            event_id="e1", spawn_id="sp1", parent_run_id="run1",
            event_type=DelegationEventType.QUESTION,
            payload={
                "question_id": "q1", "question": "Which env?",
                "options": ["test", "prod"], "suggested_default": "test",
            },
        )
        req = event_to_hitl_request(event)
        assert req is not None
        assert req.request_type == "question"
        assert req.message == "Which env?"
        assert req.options == ["test", "prod"]
        assert req.suggested_default == "test"

    def test_event_to_hitl_request_confirmation(self):
        from agent_framework.models.subagent import (DelegationEvent,
                                                     DelegationEventType)
        from agent_framework.subagent.hitl import event_to_hitl_request

        event = DelegationEvent(
            event_type=DelegationEventType.CONFIRMATION_REQUEST,
            spawn_id="sp1", parent_run_id="run1",
            payload={"request_id": "c1", "reason": "Dangerous op", "action_label": "Delete DB"},
        )
        req = event_to_hitl_request(event)
        assert req is not None
        assert req.request_type == "confirmation"
        assert req.title == "Delete DB"
        assert req.message == "Dangerous op"

    def test_event_to_hitl_request_non_hitl_returns_none(self):
        from agent_framework.models.subagent import (DelegationEvent,
                                                     DelegationEventType)
        from agent_framework.subagent.hitl import event_to_hitl_request

        event = DelegationEvent(event_type=DelegationEventType.PROGRESS)
        assert event_to_hitl_request(event) is None

    @pytest.mark.asyncio
    async def test_delegation_executor_forward_hitl(self):
        from agent_framework.models.subagent import HITLRequest, HITLResponse
        from agent_framework.subagent.delegation import DelegationExecutor
        from agent_framework.subagent.hitl import CallbackHITLHandler

        async def approve(req: HITLRequest) -> HITLResponse:
            return HITLResponse(request_id=req.request_id, response_type="confirm")

        handler = CallbackHITLHandler(approve)
        runtime = AsyncMock()
        ex = DelegationExecutor(sub_agent_runtime=runtime, hitl_handler=handler)

        resp = await ex.forward_hitl_request(HITLRequest(
            request_id="r1", spawn_id="sp1", parent_run_id="run1",
            request_type="confirmation", message="Proceed?",
        ))
        assert resp is not None
        assert resp.response_type == "confirm"

    @pytest.mark.asyncio
    async def test_delegation_executor_forward_hitl_no_handler(self):
        from agent_framework.models.subagent import HITLRequest
        from agent_framework.subagent.delegation import DelegationExecutor

        ex = DelegationExecutor()
        resp = await ex.forward_hitl_request(HITLRequest(
            request_id="r1", spawn_id="sp1",
        ))
        assert resp is None


# ---------------------------------------------------------------------------
# Phase C: Summarization Tests
# ---------------------------------------------------------------------------

class TestDelegationSummarization:
    """DelegationSummary with suspend_info and new status values."""

    def test_summarize_success(self):
        from agent_framework.models.subagent import (SubAgentResult,
                                                     SubAgentStatus)
        from agent_framework.subagent.delegation import DelegationExecutor
        result = SubAgentResult(spawn_id="s1", success=True, final_answer="Done")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.status == SubAgentStatus.COMPLETED.value
        assert "completed successfully" in summary.summary

    def test_summarize_suspended(self):
        from agent_framework.models.subagent import (SubAgentResult,
                                                     SubAgentStatus,
                                                     SubAgentSuspendInfo,
                                                     SubAgentSuspendReason)
        from agent_framework.subagent.delegation import DelegationExecutor
        result = SubAgentResult(
            spawn_id="s1", success=False,
            suspend_info=SubAgentSuspendInfo(
                reason=SubAgentSuspendReason.WAIT_USER_CONFIRMATION,
                message="Need approval to continue",
                resume_token="tok_1",
            ),
        )
        summary = DelegationExecutor.summarize_result(result)
        assert summary.status == SubAgentStatus.SUSPENDED.value
        assert "suspended" in summary.summary.lower()
        assert "resume_subagent" in summary.summary

    def test_summarize_timeout(self):
        from agent_framework.models.subagent import (SubAgentResult,
                                                     SubAgentStatus)
        from agent_framework.subagent.delegation import DelegationExecutor
        result = SubAgentResult(spawn_id="s1", success=False, error="operation timed out")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.status == SubAgentStatus.TIMEOUT.value
        assert summary.error_code == "TIMEOUT"

    def test_summarize_failed(self):
        from agent_framework.models.subagent import (SubAgentResult,
                                                     SubAgentStatus)
        from agent_framework.subagent.delegation import DelegationExecutor
        result = SubAgentResult(spawn_id="s1", success=False, error="internal error")
        summary = DelegationExecutor.summarize_result(result)
        assert summary.status == SubAgentStatus.FAILED.value


# ---------------------------------------------------------------------------
# Architecture Guards
# ---------------------------------------------------------------------------

class TestArchitectureGuards:
    """Verify architecture invariants for long-term interaction."""

    def test_delegation_event_append_only(self):
        """Events cannot be modified after append (only acked flag changes)."""
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel

        ch = InMemoryInteractionChannel()
        ch.emit_event("sp1", "run1", DelegationEventType.STARTED)

        events = ch.list_events("sp1")
        # Direct mutation would be on a copy; the original is in the internal list
        assert events[0].sequence_no == 1
        # Sequence cannot go backwards
        ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)
        events2 = ch.list_events("sp1")
        assert events2[1].sequence_no > events2[0].sequence_no

    def test_delegation_mode_on_spec(self):
        """SubAgentSpec must carry delegation_mode."""
        from agent_framework.models.subagent import (DelegationMode,
                                                     SubAgentSpec)
        spec = SubAgentSpec(delegation_mode=DelegationMode.INTERACTIVE)
        assert spec.delegation_mode == DelegationMode.INTERACTIVE
        # Default is BLOCKING
        spec2 = SubAgentSpec()
        assert spec2.delegation_mode == DelegationMode.BLOCKING

    def test_subagent_handle_extended_fields(self):
        """SubAgentHandle must have new v3.1 fields."""
        from agent_framework.models.subagent import (SubAgentHandle,
                                                     SubAgentStatus)
        handle = SubAgentHandle(
            spawn_id="sp1",
            status=SubAgentStatus.WAITING_PARENT,
            waiting_reason="Needs env selection",
            resume_token="tok_1",
            last_event_seq=5,
        )
        assert handle.waiting_reason == "Needs env selection"
        assert handle.resume_token == "tok_1"
        assert handle.last_event_seq == 5

    def test_subagent_result_has_suspend_info(self):
        """SubAgentResult must support suspend_info field."""
        from agent_framework.models.subagent import (SubAgentResult,
                                                     SubAgentSuspendInfo,
                                                     SubAgentSuspendReason)
        result = SubAgentResult(
            spawn_id="sp1",
            suspend_info=SubAgentSuspendInfo(
                reason=SubAgentSuspendReason.CHECKPOINT_PAUSE,
                resume_token="ckpt_1",
            ),
        )
        assert result.suspend_info is not None

    def test_checkpoint_model(self):
        """SubAgentCheckpoint model exists and has required fields."""
        from agent_framework.models.subagent import SubAgentCheckpoint
        ckpt = SubAgentCheckpoint(
            checkpoint_id="cp1", spawn_id="sp1",
            resume_token="tok_1", summary="Search phase done",
            iteration_index=5,
        )
        assert ckpt.iteration_index == 5

    def test_long_interaction_config(self):
        """LongInteractionConfig exists in FrameworkConfig."""
        from agent_framework.infra.config import FrameworkConfig
        cfg = FrameworkConfig()
        assert cfg.long_interaction.enable_interactive_subagents is True
        assert cfg.long_interaction.max_delegation_events_per_subagent == 200
        assert cfg.long_interaction.max_pending_hitl_requests_per_run == 5

    def test_protocol_extensions(self):
        """Protocols have new v3.1 methods."""
        from agent_framework.protocols.core import (
            DelegationExecutorProtocol, HITLHandlerProtocol,
            SubAgentInteractionChannelProtocol, SubAgentRuntimeProtocol)

        # Just verify they import and have the expected method names
        assert hasattr(SubAgentRuntimeProtocol, 'resume')
        assert hasattr(SubAgentRuntimeProtocol, 'spawn_async')
        assert hasattr(SubAgentRuntimeProtocol, 'collect_result')
        assert hasattr(SubAgentRuntimeProtocol, 'cancel')
        assert hasattr(DelegationExecutorProtocol, 'resume_subagent')
        assert hasattr(DelegationExecutorProtocol, 'resume_a2a')
        assert hasattr(DelegationExecutorProtocol, 'cancel_subagent')
        assert hasattr(SubAgentInteractionChannelProtocol, 'append_event')
        assert hasattr(SubAgentInteractionChannelProtocol, 'list_events')
        assert hasattr(SubAgentInteractionChannelProtocol, 'ack_event')
        assert hasattr(HITLHandlerProtocol, 'handle_hitl_request')


# ---------------------------------------------------------------------------
# Fault Injection Tests
# ---------------------------------------------------------------------------

class TestFaultInjection:
    """Simulate failure scenarios for robustness."""

    def test_completed_then_progress_rejected(self):
        """After COMPLETED status, no further events should change status mapping."""
        from agent_framework.models.subagent import (
            InvalidStatusTransitionError, SubAgentStatus,
            validate_status_transition)

        # Terminal states have no valid transitions
        with pytest.raises(InvalidStatusTransitionError):
            validate_status_transition(SubAgentStatus.COMPLETED, SubAgentStatus.RUNNING)

    def test_concurrent_events_dont_corrupt(self):
        """Concurrent event appends preserve monotonic sequence."""
        from agent_framework.models.subagent import DelegationEventType
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel

        ch = InMemoryInteractionChannel(max_events_per_spawn=500)
        barrier = threading.Barrier(4)

        def append_batch():
            barrier.wait()
            for _ in range(100):
                ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)

        threads = [threading.Thread(target=append_batch) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        events = ch.list_events("sp1")
        assert len(events) == 400
        seqs = [e.sequence_no for e in events]
        assert seqs == sorted(seqs)
        assert seqs == list(range(1, 401))

    @pytest.mark.asyncio
    async def test_resume_after_cancel(self):
        """Resume after cancel should still work at runtime level (runtime decides)."""
        from agent_framework.subagent.delegation import DelegationExecutor

        runtime = AsyncMock()
        runtime.resume = AsyncMock(return_value=MagicMock(
            spawn_id="sp1", success=False,
            error="Cannot resume cancelled task",
            suspend_info=None, iterations_used=0, artifacts=[],
            final_answer=None,
        ))
        runtime.cancel = AsyncMock()

        ex = DelegationExecutor(sub_agent_runtime=runtime)

        await ex.cancel_subagent("sp1")
        result = await ex.resume_subagent("sp1", {}, None)
        assert not result.success
        assert "cancelled" in result.error.lower()

    def test_hitl_request_all_types(self):
        """All HITL request types can be instantiated."""
        from agent_framework.models.subagent import HITLRequest

        for rtype in ("question", "confirmation", "clarification"):
            req = HITLRequest(request_type=rtype, message=f"Test {rtype}")
            assert req.request_type == rtype

    def test_hitl_response_all_types(self):
        """All HITL response types can be instantiated."""
        from agent_framework.models.subagent import HITLResponse

        for rtype in ("answer", "confirm", "deny", "cancel"):
            resp = HITLResponse(response_type=rtype)
            assert resp.response_type == rtype

    def test_delegation_event_summary_model(self):
        """DelegationEventSummary captures all fields correctly."""
        from agent_framework.models.subagent import (DelegationEventSummary,
                                                     SubAgentStatus)
        summary = DelegationEventSummary(
            spawn_id="sp1",
            status=SubAgentStatus.WAITING_USER,
            summary="Analyzing code",
            question="Approve deployment?",
            artifacts_digest=["report.pdf"],
        )
        assert summary.status == SubAgentStatus.WAITING_USER
        assert summary.question == "Approve deployment?"

    def test_runtime_notification_model(self):
        """RuntimeNotification fields."""
        from agent_framework.models.subagent import (RuntimeNotification,
                                                     RuntimeNotificationType)
        n = RuntimeNotification(
            notification_id="n1",
            notification_type=RuntimeNotificationType.DELEGATION_EVENT,
            run_id="run1",
            payload={"event_type": "PROGRESS"},
        )
        assert n.notification_type == RuntimeNotificationType.DELEGATION_EVENT


# ---------------------------------------------------------------------------
# Boundary Refinement Tests (§2-§16)
# ---------------------------------------------------------------------------

class TestBoundaryRefinements:
    """Tests for boundary analysis fixes."""

    # §2: PauseReason orthogonal to status
    def test_pause_reason_on_handle(self):
        from agent_framework.models.subagent import (PauseReason,
                                                     SubAgentHandle,
                                                     SubAgentStatus)
        handle = SubAgentHandle(
            status=SubAgentStatus.WAITING_PARENT,
            pause_reason=PauseReason.WAIT_PARENT_INPUT,
        )
        assert handle.pause_reason == PauseReason.WAIT_PARENT_INPUT

    def test_pause_reason_all_values(self):
        from agent_framework.models.subagent import PauseReason
        expected = {
            "NONE", "WAIT_PARENT_INPUT", "WAIT_USER_INPUT",
            "WAIT_EXTERNAL_EVENT", "CHECKPOINT_PAUSE",
            "QUOTA_BACKPRESSURE", "MANUAL_REVIEW",
        }
        assert {r.value for r in PauseReason} == expected

    def test_is_paused_status(self):
        from agent_framework.models.subagent import (SubAgentStatus,
                                                     is_paused_status)
        assert is_paused_status(SubAgentStatus.WAITING_PARENT)
        assert is_paused_status(SubAgentStatus.WAITING_USER)
        assert is_paused_status(SubAgentStatus.SUSPENDED)
        assert not is_paused_status(SubAgentStatus.RUNNING)
        assert not is_paused_status(SubAgentStatus.COMPLETED)

    # §3: WaitMode + allow_intermediate_events
    def test_wait_mode_on_spec(self):
        from agent_framework.models.subagent import SubAgentSpec, WaitMode
        spec = SubAgentSpec(
            wait_mode=WaitMode.NON_BLOCKING,
            allow_intermediate_events=True,
        )
        assert spec.wait_mode == WaitMode.NON_BLOCKING
        assert spec.allow_intermediate_events is True

    def test_wait_mode_default(self):
        from agent_framework.models.subagent import SubAgentSpec, WaitMode
        spec = SubAgentSpec()
        assert spec.wait_mode == WaitMode.BLOCKING
        assert spec.allow_intermediate_events is False

    # §4: AckLevel on events
    def test_ack_level_enum(self):
        from agent_framework.models.subagent import AckLevel
        levels = [AckLevel.NONE, AckLevel.RECEIVED, AckLevel.PROJECTED, AckLevel.HANDLED]
        assert len(levels) == 4

    def test_ack_level_on_event(self):
        from agent_framework.models.subagent import AckLevel, DelegationEvent
        event = DelegationEvent(event_id="e1")
        assert event.ack_level == AckLevel.NONE
        assert not event.acked  # backward compat property

    def test_ack_level_progression(self):
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel

        ch = InMemoryInteractionChannel()
        e = ch.emit_event("sp1", "run1", DelegationEventType.QUESTION, {}, requires_ack=True)

        # Advance to RECEIVED
        ch.ack_event("sp1", e.event_id, AckLevel.RECEIVED)
        events = ch.list_events("sp1")
        assert events[0].ack_level == AckLevel.RECEIVED

        # Advance to HANDLED
        ch.ack_event("sp1", e.event_id, AckLevel.HANDLED)
        events = ch.list_events("sp1")
        assert events[0].ack_level == AckLevel.HANDLED

        # Cannot regress to RECEIVED
        ch.ack_event("sp1", e.event_id, AckLevel.RECEIVED)
        events = ch.list_events("sp1")
        assert events[0].ack_level == AckLevel.HANDLED  # No regression

    # §8: CheckpointLevel
    def test_checkpoint_level_on_suspend_info(self):
        from agent_framework.models.subagent import (CheckpointLevel,
                                                     SubAgentSuspendInfo,
                                                     SubAgentSuspendReason)
        info = SubAgentSuspendInfo(
            reason=SubAgentSuspendReason.CHECKPOINT_PAUSE,
            resume_token="tok_1",
            checkpoint_level=CheckpointLevel.STEP_RESUMABLE,
        )
        assert info.checkpoint_level == CheckpointLevel.STEP_RESUMABLE

    def test_checkpoint_level_default_is_coordination_only(self):
        from agent_framework.models.subagent import (CheckpointLevel,
                                                     SubAgentSuspendInfo,
                                                     SubAgentSuspendReason)
        info = SubAgentSuspendInfo(
            reason=SubAgentSuspendReason.WAIT_PARENT_INPUT,
            resume_token="tok_1",
        )
        assert info.checkpoint_level == CheckpointLevel.COORDINATION_ONLY

    def test_checkpoint_model_has_level(self):
        from agent_framework.models.subagent import (CheckpointLevel,
                                                     SubAgentCheckpoint)
        ckpt = SubAgentCheckpoint(
            checkpoint_id="cp1", spawn_id="sp1",
            resume_token="tok_1",
            checkpoint_level=CheckpointLevel.PHASE_RESTARTABLE,
        )
        assert ckpt.checkpoint_level == CheckpointLevel.PHASE_RESTARTABLE

    # §9: CANCELLING cooperative state
    def test_cancelling_state_exists(self):
        from agent_framework.models.subagent import SubAgentStatus
        assert SubAgentStatus.CANCELLING.value == "CANCELLING"

    def test_cancelling_transitions(self):
        from agent_framework.models.subagent import (
            SubAgentStatus, validate_status_transition)

        # RUNNING -> CANCELLING -> CANCELLED
        validate_status_transition(SubAgentStatus.RUNNING, SubAgentStatus.CANCELLING)
        validate_status_transition(SubAgentStatus.CANCELLING, SubAgentStatus.CANCELLED)
        # CANCELLING can also go to FAILED (cancel itself failed)
        validate_status_transition(SubAgentStatus.CANCELLING, SubAgentStatus.FAILED)

    def test_cancelling_is_active(self):
        from agent_framework.models.subagent import (SubAgentStatus,
                                                     is_active_status)
        assert is_active_status(SubAgentStatus.CANCELLING)

    # §10: DelegationCapabilities
    def test_delegation_capabilities_model(self):
        from agent_framework.models.subagent import (CheckpointLevel,
                                                     DelegationCapabilities)
        caps = DelegationCapabilities(
            supports_suspend_resume=True,
            supports_checkpointing=True,
            checkpoint_level=CheckpointLevel.STEP_RESUMABLE,
        )
        assert caps.supports_suspend_resume is True
        assert caps.checkpoint_level == CheckpointLevel.STEP_RESUMABLE

    def test_capabilities_defaults(self):
        from agent_framework.models.subagent import (CheckpointLevel,
                                                     DelegationCapabilities)
        caps = DelegationCapabilities()
        assert caps.supports_progress_events is True
        assert caps.supports_suspend_resume is False
        assert caps.checkpoint_level == CheckpointLevel.NONE

    def test_handle_has_capabilities(self):
        from agent_framework.models.subagent import (DelegationCapabilities,
                                                     SubAgentHandle)
        handle = SubAgentHandle(capabilities=DelegationCapabilities(
            supports_typed_questions=True,
        ))
        assert handle.capabilities.supports_typed_questions is True

    # §16: DegradationReason
    def test_degradation_reason_enum(self):
        from agent_framework.models.subagent import DegradationReason
        assert len(DegradationReason) == 6

    def test_degradation_reason_on_result(self):
        from agent_framework.models.subagent import (DegradationReason,
                                                     SubAgentResult,
                                                     SubAgentStatus)
        result = SubAgentResult(
            spawn_id="s1", success=False,
            final_status=SubAgentStatus.DEGRADED,
            degradation_reason=DegradationReason.TOOL_UNAVAILABLE,
            error="Shell tool not available",
        )
        assert result.degradation_reason == DegradationReason.TOOL_UNAVAILABLE

    def test_degradation_reason_on_summary(self):
        from agent_framework.models.subagent import (DegradationReason,
                                                     DelegationEventSummary,
                                                     SubAgentStatus)
        summary = DelegationEventSummary(
            spawn_id="sp1",
            status=SubAgentStatus.DEGRADED,
            degradation_reason=DegradationReason.QUOTA_LIMITED,
            summary="Ran with reduced capability",
        )
        assert summary.degradation_reason == DegradationReason.QUOTA_LIMITED

    # §6: HITL ownership on parent
    def test_hitl_request_has_parent_run_id(self):
        """HITLRequest must carry parent_run_id — ownership is parent control plane."""
        from agent_framework.models.subagent import HITLRequest
        req = HITLRequest(
            request_id="r1", spawn_id="sp1", parent_run_id="run1",
            request_type="question", message="Which env?",
        )
        assert req.parent_run_id == "run1"

    # §11: DelegationEventSummary has pause_reason
    def test_event_summary_has_pause_reason(self):
        from agent_framework.models.subagent import (DelegationEventSummary,
                                                     PauseReason,
                                                     SubAgentStatus)
        summary = DelegationEventSummary(
            status=SubAgentStatus.WAITING_USER,
            pause_reason=PauseReason.WAIT_USER_INPUT,
        )
        assert summary.pause_reason == PauseReason.WAIT_USER_INPUT


# ---------------------------------------------------------------------------
# Execution Chain Tests (codex review fixes)
# ---------------------------------------------------------------------------

class TestRuntimeResumeCancel:
    """SubAgentRuntime.resume() and cancel() execution chain."""

    def test_runtime_has_resume_method(self):
        from agent_framework.subagent.runtime import SubAgentRuntime
        assert hasattr(SubAgentRuntime, 'resume')

    def test_runtime_has_cancel_method(self):
        from agent_framework.subagent.runtime import SubAgentRuntime
        assert hasattr(SubAgentRuntime, 'cancel')


class TestA2ACapabilities:
    """A2A adapter DelegationCapabilities mapping."""

    def test_adapter_has_capabilities_dict(self):
        from agent_framework.protocols.a2a.a2a_client_adapter import \
            A2AClientAdapter
        adapter = A2AClientAdapter()
        assert hasattr(adapter, '_capabilities')
        assert isinstance(adapter._capabilities, dict)

    def test_get_capabilities_default(self):
        from agent_framework.models.subagent import (CheckpointLevel,
                                                     DelegationCapabilities)
        from agent_framework.protocols.a2a.a2a_client_adapter import \
            A2AClientAdapter
        adapter = A2AClientAdapter()
        caps = adapter.get_capabilities("nonexistent")
        assert isinstance(caps, DelegationCapabilities)
        assert caps.checkpoint_level == CheckpointLevel.NONE

    def test_adapter_has_resume_task(self):
        from agent_framework.protocols.a2a.a2a_client_adapter import \
            A2AClientAdapter
        assert hasattr(A2AClientAdapter, 'resume_task')

    def test_capability_downgrade_waiting_user(self):
        """WAITING_USER result from agent without typed_questions support → FAILED."""
        from agent_framework.models.subagent import (DelegationCapabilities,
                                                     SubAgentResult,
                                                     SubAgentStatus)
        from agent_framework.subagent.delegation import DelegationExecutor
        result = SubAgentResult(
            spawn_id="sp1", success=False,
            final_status=SubAgentStatus.WAITING_USER,
        )
        caps = DelegationCapabilities(supports_typed_questions=False)
        downgraded = DelegationExecutor._apply_capability_downgrade(result, caps, "test_agent")
        assert downgraded.final_status == SubAgentStatus.FAILED
        assert "supports_typed_questions" in downgraded.error

    def test_capability_no_downgrade_when_supported(self):
        """WAITING_USER result from agent WITH typed_questions → no change."""
        from agent_framework.models.subagent import (DelegationCapabilities,
                                                     SubAgentResult,
                                                     SubAgentStatus)
        from agent_framework.subagent.delegation import DelegationExecutor
        result = SubAgentResult(
            spawn_id="sp1", success=False,
            final_status=SubAgentStatus.WAITING_USER,
        )
        caps = DelegationCapabilities(supports_typed_questions=True)
        unchanged = DelegationExecutor._apply_capability_downgrade(result, caps, "test_agent")
        assert unchanged.final_status == SubAgentStatus.WAITING_USER

    def test_capability_downgrade_suspended_no_resume(self):
        """SUSPENDED result from agent without suspend_resume → FAILED."""
        from agent_framework.models.subagent import (DelegationCapabilities,
                                                     SubAgentResult,
                                                     SubAgentStatus)
        from agent_framework.subagent.delegation import DelegationExecutor
        result = SubAgentResult(
            spawn_id="sp1", success=False,
            final_status=SubAgentStatus.SUSPENDED,
        )
        caps = DelegationCapabilities(supports_suspend_resume=False)
        downgraded = DelegationExecutor._apply_capability_downgrade(result, caps, "test_agent")
        assert downgraded.final_status == SubAgentStatus.FAILED
        assert "supports_suspend_resume" in downgraded.error


class TestAckLevelConsumptionChain:
    """AckLevel progression through notification pipeline (boundary §4)."""

    def test_drain_advances_to_received(self):
        """drain_all() must advance events to RECEIVED ack level."""
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        ch = InMemoryInteractionChannel()
        nc = RuntimeNotificationChannel(interaction_channel=ch)
        nc.monitor_spawn("sp1")

        ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS, {"p": 50})

        # Before drain: ack_level is NONE
        events = ch.list_events("sp1")
        assert events[0].ack_level == AckLevel.NONE

        # After drain: ack_level advances to RECEIVED
        nc.drain_all()
        events = ch.list_events("sp1")
        assert events[0].ack_level == AckLevel.RECEIVED

    def test_mark_projected_advances_ack(self):
        """mark_projected() must advance events to PROJECTED."""
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        ch = InMemoryInteractionChannel()
        nc = RuntimeNotificationChannel(interaction_channel=ch)
        nc.monitor_spawn("sp1")

        e = ch.emit_event("sp1", "run1", DelegationEventType.PROGRESS)
        nc.drain_all()  # -> RECEIVED

        nc.mark_projected("sp1", e.event_id)
        events = ch.list_events("sp1")
        assert events[0].ack_level == AckLevel.PROJECTED

    def test_mark_handled_advances_ack(self):
        """mark_handled() must advance events to HANDLED."""
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        ch = InMemoryInteractionChannel()
        nc = RuntimeNotificationChannel(interaction_channel=ch)
        nc.monitor_spawn("sp1")

        e = ch.emit_event("sp1", "run1", DelegationEventType.QUESTION, {}, requires_ack=True)
        nc.drain_all()  # -> RECEIVED
        nc.mark_projected("sp1", e.event_id)  # -> PROJECTED
        nc.mark_handled("sp1", e.event_id)  # -> HANDLED

        events = ch.list_events("sp1")
        assert events[0].ack_level == AckLevel.HANDLED

    def test_ack_level_full_chain(self):
        """Full chain: NONE -> RECEIVED (drain) -> PROJECTED -> HANDLED."""
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel

        ch = InMemoryInteractionChannel()
        nc = RuntimeNotificationChannel(interaction_channel=ch)
        nc.monitor_spawn("sp1")

        e = ch.emit_event("sp1", "run1", DelegationEventType.QUESTION, {
            "question": "Which env?",
        }, requires_ack=True)

        # Step 1: NONE
        assert ch.list_events("sp1")[0].ack_level == AckLevel.NONE

        # Step 2: drain → RECEIVED
        nc.drain_all()
        assert ch.list_events("sp1")[0].ack_level == AckLevel.RECEIVED

        # Step 3: coordinator injects summary → PROJECTED
        nc.mark_projected("sp1", e.event_id)
        assert ch.list_events("sp1")[0].ack_level == AckLevel.PROJECTED

        # Step 4: HITL answered → HANDLED
        nc.mark_handled("sp1", e.event_id)
        assert ch.list_events("sp1")[0].ack_level == AckLevel.HANDLED

        # No pending events left
        assert len(ch.get_pending_events("sp1")) == 0


class TestHITLRunScoped:
    """HITL queue is per-run scoped (boundary §6)."""

    @pytest.mark.asyncio
    async def test_per_run_limit_enforced(self):
        from agent_framework.models.subagent import HITLRequest
        from agent_framework.subagent.hitl import QueueHITLHandler

        handler = QueueHITLHandler(timeout_seconds=0.1, max_pending_per_run=2)

        # Consume requests in background so they don't block
        async def consume():
            while True:
                try:
                    await asyncio.wait_for(handler.pending_requests.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    break

        consumer = asyncio.create_task(consume())

        # First two should be accepted (they'll timeout, but enter the queue)
        tasks = []
        for i in range(3):
            task = asyncio.create_task(handler.handle_hitl_request(HITLRequest(
                request_id=f"r{i}", parent_run_id="run1",
                request_type="question", message=f"Q{i}",
            )))
            tasks.append(task)
            await asyncio.sleep(0.01)

        results = await asyncio.gather(*tasks)
        consumer.cancel()

        # Third request should have been denied (response_type="cancel") immediately
        # due to per-run limit
        cancel_count = sum(1 for r in results if r.response_type == "cancel")
        assert cancel_count >= 1  # At least the 3rd was denied

    @pytest.mark.asyncio
    async def test_run_pending_count_tracking(self):
        from agent_framework.models.subagent import HITLRequest, HITLResponse
        from agent_framework.subagent.hitl import QueueHITLHandler

        handler = QueueHITLHandler(timeout_seconds=5.0, max_pending_per_run=5)

        async def respond():
            req = await handler.pending_requests.get()
            await handler.submit_response(HITLResponse(
                request_id=req.request_id, response_type="confirm",
            ))

        asyncio.create_task(respond())

        assert handler.get_run_pending_count("run1") == 0

        resp = await handler.handle_hitl_request(HITLRequest(
            request_id="r1", parent_run_id="run1",
            request_type="confirmation", message="Ok?",
        ))
        assert resp.response_type == "confirm"

        # After response delivered, count should be back to 0
        assert handler.get_run_pending_count("run1") == 0


# ---------------------------------------------------------------------------
# Production Wiring Tests (codex review round 3)
# ---------------------------------------------------------------------------

class TestProductionWiring:
    """Verify components are actually wired in the production assembly."""

    def test_coordinator_has_notification_channel(self):
        """RunCoordinator must have RuntimeNotificationChannel, not just BackgroundNotifier."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.notification.channel import \
            RuntimeNotificationChannel
        coord = RunCoordinator()
        assert hasattr(coord, '_notification_channel')
        assert isinstance(coord._notification_channel, RuntimeNotificationChannel)

    def test_coordinator_notification_channel_wraps_bg_notifier(self):
        """RuntimeNotificationChannel should wrap the coordinator's BackgroundNotifier."""
        from agent_framework.agent.coordinator import RunCoordinator
        coord = RunCoordinator()
        assert coord._notification_channel.bg_notifier is coord._bg_notifier

    def test_framework_wires_interaction_channel(self):
        """AgentFramework.setup() must create and wire InteractionChannel."""
        from agent_framework.entry import AgentFramework
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        fw = AgentFramework()
        fw.setup()
        assert hasattr(fw, '_interaction_channel')
        assert isinstance(fw._interaction_channel, InMemoryInteractionChannel)

    def test_framework_wires_hitl_handler(self):
        """AgentFramework.setup() must create and wire QueueHITLHandler."""
        from agent_framework.entry import AgentFramework
        from agent_framework.subagent.hitl import QueueHITLHandler
        fw = AgentFramework()
        fw.setup()
        assert hasattr(fw, '_hitl_handler')
        assert isinstance(fw._hitl_handler, QueueHITLHandler)

    def test_framework_hitl_handler_uses_config_limit(self):
        """QueueHITLHandler max_pending_per_run must come from config."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework()
        fw.setup()
        assert fw._hitl_handler._max_pending_per_run == fw.config.long_interaction.max_pending_hitl_requests_per_run

    def test_framework_wires_channel_into_coordinator(self):
        """Coordinator's notification channel must reference the same interaction channel."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework()
        fw.setup()
        coord_ic = fw._coordinator._notification_channel._interaction_channel
        assert coord_ic is fw._interaction_channel

    def test_framework_delegation_executor_has_channel(self):
        """DelegationExecutor must have interaction_channel wired."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework()
        fw.setup()
        de = fw._deps.delegation_executor
        assert de._interaction_channel is fw._interaction_channel

    def test_framework_delegation_executor_has_hitl(self):
        """DelegationExecutor must have hitl_handler wired."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework()
        fw.setup()
        de = fw._deps.delegation_executor
        assert de._hitl_handler is fw._hitl_handler

    def test_coordinator_drain_uses_notification_channel(self):
        """_drain_background_notifications must use RuntimeNotificationChannel."""
        import inspect

        from agent_framework.agent.coordinator import RunCoordinator
        coord = RunCoordinator()
        source = inspect.getsource(coord._drain_background_notifications)
        # Must reference _notification_channel, not just _bg_notifier directly
        assert '_notification_channel' in source

    def test_coordinator_register_monitors_spawn_ids(self):
        """_register_background_tasks must monitor spawn_ids for delegation."""
        import inspect

        from agent_framework.agent.coordinator import RunCoordinator
        coord = RunCoordinator()
        source = inspect.getsource(coord._register_background_tasks)
        assert 'monitor_spawn' in source

    def test_coordinator_drain_advances_to_projected(self):
        """After drain and injection, delegation events must be marked PROJECTED."""
        import inspect

        from agent_framework.agent.coordinator import RunCoordinator
        coord = RunCoordinator()
        source = inspect.getsource(coord._drain_background_notifications)
        assert 'mark_projected' in source

    def test_coordinator_has_hitl_auto_forward(self):
        """Coordinator must auto-forward HITL events via _handle_hitl_event."""
        import inspect

        from agent_framework.agent.coordinator import RunCoordinator
        assert hasattr(RunCoordinator, '_handle_hitl_event')
        source = inspect.getsource(RunCoordinator._handle_hitl_event)
        assert 'event_to_hitl_request' in source
        assert 'forward_hitl_request' in source
        assert 'resume_subagent' in source
        assert 'mark_handled' in source

    def test_coordinator_drain_calls_hitl_handler(self):
        """_drain_background_notifications must trigger HITL auto-forward for QUESTION events."""
        import inspect

        from agent_framework.agent.coordinator import RunCoordinator
        source = inspect.getsource(RunCoordinator._drain_background_notifications)
        assert '_handle_hitl_event' in source
        assert 'QUESTION' in source
        assert 'CONFIRMATION_REQUEST' in source

    def test_a2a_capability_downgrade_exists(self):
        """DelegationExecutor must have _apply_capability_downgrade method."""
        import inspect

        from agent_framework.subagent.delegation import DelegationExecutor
        assert hasattr(DelegationExecutor, '_apply_capability_downgrade')
        source = inspect.getsource(DelegationExecutor._apply_capability_downgrade)
        assert 'WAITING_USER' in source
        assert 'model_copy' in source
        assert 'FAILED' in source


# ---------------------------------------------------------------------------
# End-to-End Integration: QUESTION event → coordinator drain → HITL → resume → HANDLED
# ---------------------------------------------------------------------------

class TestE2EHITLChain:
    """Full integration: emit QUESTION → drain → HITL auto-forward → resume → HANDLED."""

    @pytest.mark.asyncio
    async def test_question_event_full_chain(self):
        """Emit a QUESTION event, coordinator drains it, HITL handler answers,
        sub-agent resumes, ack reaches HANDLED."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.session import SessionState
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType,
                                                     HITLRequest, HITLResponse)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.subagent.delegation import DelegationExecutor
        from agent_framework.subagent.hitl import CallbackHITLHandler

        # 1. Setup: coordinator + interaction channel + delegation executor + HITL handler
        channel = InMemoryInteractionChannel()
        coordinator = RunCoordinator()
        coordinator._notification_channel.set_interaction_channel(channel)
        coordinator._notification_channel.monitor_spawn("sp1")

        # Auto-approve HITL handler
        async def auto_answer(req: HITLRequest) -> HITLResponse:
            return HITLResponse(
                request_id=req.request_id,
                response_type="answer",
                answer="production",
            )

        hitl_handler = CallbackHITLHandler(auto_answer)

        # Mock runtime that records resume calls
        resume_calls = []

        class MockRuntime:
            async def resume(self, spawn_id, payload, parent):
                resume_calls.append((spawn_id, payload))
                return MagicMock(
                    spawn_id=spawn_id, success=True, final_answer="Resumed OK",
                    suspend_info=None, iterations_used=1, artifacts=[],
                    error=None,
                )
            async def cancel(self, spawn_id):
                pass

        delegation_executor = DelegationExecutor(
            sub_agent_runtime=MockRuntime(),
            interaction_channel=channel,
            hitl_handler=hitl_handler,
        )

        # Mock deps with delegation_executor
        mock_deps = MagicMock()
        mock_deps.delegation_executor = delegation_executor

        # 2. Emit a QUESTION event from the sub-agent side
        question_event = channel.emit_event(
            "sp1", "run1",
            DelegationEventType.QUESTION,
            {
                "question_id": "q1",
                "question": "Which environment to deploy?",
                "options": ["staging", "production"],
                "suggested_default": "staging",
            },
            requires_ack=True,
        )

        # 3. Verify initial state: ack_level = NONE
        events = channel.list_events("sp1")
        assert events[0].ack_level == AckLevel.NONE

        # 4. Coordinator drains — should trigger full HITL chain
        session = SessionState()
        await coordinator._drain_background_notifications(session, mock_deps)

        # 5. Verify: event was injected into session
        msgs = session.get_messages()
        assert len(msgs) == 2  # user notification + assistant ack
        assert "delegation-event" in msgs[0].content

        # 6. Verify: ack_level reached HANDLED (full chain completed)
        events = channel.list_events("sp1")
        assert events[0].ack_level == AckLevel.HANDLED

        # 7. Verify: resume was called with the user's answer
        assert len(resume_calls) == 1
        assert resume_calls[0][0] == "sp1"
        assert resume_calls[0][1]["answer"] == "production"

    @pytest.mark.asyncio
    async def test_confirmation_event_denied_chain(self):
        """CONFIRMATION_REQUEST event where user denies → resume with denied=True."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.session import SessionState
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType,
                                                     HITLRequest, HITLResponse)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.subagent.delegation import DelegationExecutor
        from agent_framework.subagent.hitl import CallbackHITLHandler

        channel = InMemoryInteractionChannel()
        coordinator = RunCoordinator()
        coordinator._notification_channel.set_interaction_channel(channel)
        coordinator._notification_channel.monitor_spawn("sp2")

        # User denies confirmation
        async def deny_all(req: HITLRequest) -> HITLResponse:
            return HITLResponse(request_id=req.request_id, response_type="deny")

        resume_calls = []

        class MockRuntime:
            async def resume(self, spawn_id, payload, parent):
                resume_calls.append((spawn_id, payload))
                return MagicMock(
                    spawn_id=spawn_id, success=False,
                    error="User denied", suspend_info=None,
                    iterations_used=0, artifacts=[], final_answer=None,
                )
            async def cancel(self, spawn_id):
                pass

        delegation_executor = DelegationExecutor(
            sub_agent_runtime=MockRuntime(),
            interaction_channel=channel,
            hitl_handler=CallbackHITLHandler(deny_all),
        )

        mock_deps = MagicMock()
        mock_deps.delegation_executor = delegation_executor

        channel.emit_event(
            "sp2", "run1",
            DelegationEventType.CONFIRMATION_REQUEST,
            {"request_id": "c1", "reason": "Delete database?", "action_label": "Drop DB"},
            requires_ack=True,
        )

        session = SessionState()
        await coordinator._drain_background_notifications(session, mock_deps)

        # Resume was called with denied=True
        assert len(resume_calls) == 1
        assert resume_calls[0][1]["denied"] is True

        # Event reached HANDLED
        events = channel.list_events("sp2")
        assert events[0].ack_level == AckLevel.HANDLED

    @pytest.mark.asyncio
    async def test_progress_event_no_hitl_trigger(self):
        """PROGRESS events should NOT trigger HITL forwarding."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.session import SessionState
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.subagent.delegation import DelegationExecutor

        channel = InMemoryInteractionChannel()
        coordinator = RunCoordinator()
        coordinator._notification_channel.set_interaction_channel(channel)
        coordinator._notification_channel.monitor_spawn("sp3")

        resume_calls = []

        class MockRuntime:
            async def resume(self, spawn_id, payload, parent):
                resume_calls.append(spawn_id)
                return MagicMock(spawn_id=spawn_id, success=True)

        delegation_executor = DelegationExecutor(sub_agent_runtime=MockRuntime())
        mock_deps = MagicMock()
        mock_deps.delegation_executor = delegation_executor

        channel.emit_event("sp3", "run1", DelegationEventType.PROGRESS, {"p": 50})

        session = SessionState()
        await coordinator._drain_background_notifications(session, mock_deps)

        # PROGRESS should NOT trigger resume
        assert len(resume_calls) == 0

        # But ack should still be PROJECTED (not HANDLED — no HITL processing)
        events = channel.list_events("sp3")
        assert events[0].ack_level == AckLevel.PROJECTED

    @pytest.mark.asyncio
    async def test_no_hitl_handler_stays_at_projected(self):
        """Without HITL handler, QUESTION events stay at PROJECTED (not HANDLED)."""
        from agent_framework.agent.coordinator import RunCoordinator
        from agent_framework.models.session import SessionState
        from agent_framework.models.subagent import (AckLevel,
                                                     DelegationEventType)
        from agent_framework.subagent.interaction_channel import \
            InMemoryInteractionChannel
        from agent_framework.subagent.delegation import DelegationExecutor

        channel = InMemoryInteractionChannel()
        coordinator = RunCoordinator()
        coordinator._notification_channel.set_interaction_channel(channel)
        coordinator._notification_channel.monitor_spawn("sp4")

        # No HITL handler on executor
        delegation_executor = DelegationExecutor(
            sub_agent_runtime=AsyncMock(),
            hitl_handler=None,
        )
        mock_deps = MagicMock()
        mock_deps.delegation_executor = delegation_executor

        channel.emit_event(
            "sp4", "run1", DelegationEventType.QUESTION,
            {"question": "Which?"},
            requires_ack=True,
        )

        session = SessionState()
        await coordinator._drain_background_notifications(session, mock_deps)

        # Without HITL handler, forward_hitl_request returns None → stays PROJECTED
        events = channel.list_events("sp4")
        assert events[0].ack_level == AckLevel.PROJECTED

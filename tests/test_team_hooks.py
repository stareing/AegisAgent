"""Tests for Team Hooks — TEAMMATE_IDLE and TEAMMATE_TASK_COMPLETED.

Verifies:
1. HookPoint enum has team entries.
2. TEAMMATE_TASK_COMPLETED is deniable.
3. Payload factories produce correct structure.
4. Hook executor attribute exists on coordinator.
"""

from __future__ import annotations

import pytest

from agent_framework.models.hook import (
    DENIABLE_HOOK_POINTS,
    HookPoint,
)
from agent_framework.hooks.payloads import (
    teammate_idle_payload,
    teammate_task_completed_payload,
)


class TestHookPointEnum:
    def test_teammate_idle_exists(self):
        assert HookPoint.TEAMMATE_IDLE == "teammate.idle"

    def test_teammate_task_completed_exists(self):
        assert HookPoint.TEAMMATE_TASK_COMPLETED == "teammate.task_completed"

    def test_task_completed_is_deniable(self):
        assert HookPoint.TEAMMATE_TASK_COMPLETED in DENIABLE_HOOK_POINTS

    def test_teammate_idle_is_not_deniable(self):
        assert HookPoint.TEAMMATE_IDLE not in DENIABLE_HOOK_POINTS


class TestPayloadFactories:
    def test_teammate_idle_payload(self):
        payload = teammate_idle_payload(
            agent_id="role_coder", role="coder",
            team_id="team_abc", tasks_completed=3,
        )
        assert payload["agent_id"] == "role_coder"
        assert payload["role"] == "coder"
        assert payload["team_id"] == "team_abc"
        assert payload["tasks_completed"] == 3

    def test_teammate_task_completed_payload(self):
        payload = teammate_task_completed_payload(
            agent_id="role_reviewer", role="reviewer",
            team_id="team_abc", task_id="task_001",
            task_title="Review PR", result_summary="Looks good",
        )
        assert payload["agent_id"] == "role_reviewer"
        assert payload["task_id"] == "task_001"
        assert payload["task_title"] == "Review PR"
        assert payload["result_summary"] == "Looks good"

    def test_result_summary_truncated(self):
        long_summary = "x" * 1000
        payload = teammate_task_completed_payload(
            agent_id="a", role="r", team_id="t",
            result_summary=long_summary,
        )
        assert len(payload["result_summary"]) == 500


class TestCoordinatorHookExecutor:
    def test_coordinator_has_hook_executor_attr(self):
        from agent_framework.notification.bus import AgentBus
        from agent_framework.notification.persistence import InMemoryBusPersistence
        from agent_framework.team.coordinator import TeamCoordinator
        from agent_framework.team.mailbox import TeamMailbox
        from agent_framework.team.plan_registry import PlanRegistry
        from agent_framework.team.registry import TeamRegistry
        from agent_framework.team.shutdown_registry import ShutdownRegistry

        bus = AgentBus(persistence=InMemoryBusPersistence())
        registry = TeamRegistry("t")
        mailbox = TeamMailbox(bus, registry)
        coord = TeamCoordinator("t", "lead", mailbox, registry,
                                PlanRegistry(), ShutdownRegistry())
        assert hasattr(coord, "_hook_executor")
        assert coord._hook_executor is None  # Not wired until entry.py sets it

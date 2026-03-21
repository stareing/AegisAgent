"""Tests for AgentBus, BusPersistence backends, and topic matching.

Covers:
- InMemoryBusPersistence: store/load_pending/mark_delivered/mark_acked/get_envelope/cleanup_expired/cleanup_group
- SQLiteBusPersistence: same operations + crash recovery (close + reopen)
- AgentBus: publish/subscribe/drain/peek/ack/broadcast/send/reply/register_participant/list_participants/clear_group
- Topic matching: exact, single wildcard *, double wildcard **, mixed patterns
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from agent_framework.models.subagent import AckLevel
from agent_framework.notification.bus import AgentBus
from agent_framework.notification.envelope import BusAddress, BusEnvelope
from agent_framework.notification.persistence import (
    InMemoryBusPersistence,
    SQLiteBusPersistence,
)
from agent_framework.notification.topics import topic_matches


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _make_envelope(
    *,
    topic: str = "test.topic",
    source_id: str = "agent_a",
    target_id: str | None = "agent_b",
    group: str = "team1",
    payload: dict | None = None,
    ttl_ms: int = 0,
    priority: int = 5,
    requires_ack: bool = False,
) -> BusEnvelope:
    source = BusAddress(agent_id=source_id, group=group)
    target = BusAddress(agent_id=target_id, group=group) if target_id else None
    return BusEnvelope(
        topic=topic,
        source=source,
        target=target,
        payload=payload or {},
        ttl_ms=ttl_ms,
        priority=priority,
        requires_ack=requires_ack,
    )


# ═══════════════════════════════════════════════════════════════════
# Topic matching
# ═══════════════════════════════════════════════════════════════════


class TestTopicMatching:
    """Topic pattern matching with wildcards."""

    def test_exact_match(self) -> None:
        assert topic_matches("agent.progress", "agent.progress") is True

    def test_exact_no_match(self) -> None:
        assert topic_matches("agent.progress", "agent.error") is False

    def test_single_wildcard_match(self) -> None:
        assert topic_matches("agent.*", "agent.progress") is True

    def test_single_wildcard_no_multi_segment(self) -> None:
        assert topic_matches("agent.*", "agent.sp1.progress") is False

    def test_double_wildcard_multi_segment(self) -> None:
        assert topic_matches("agent.**", "agent.sp1.progress") is True

    def test_double_wildcard_single_segment(self) -> None:
        assert topic_matches("agent.**", "agent.progress") is True

    def test_global_wildcard(self) -> None:
        assert topic_matches("**", "anything.at.all") is True

    def test_mixed_pattern(self) -> None:
        assert topic_matches("team.*.shutdown", "team.alpha.shutdown") is True

    def test_mixed_pattern_no_match(self) -> None:
        assert topic_matches("team.*.shutdown", "team.alpha.beta.shutdown") is False

    def test_double_wildcard_middle(self) -> None:
        assert topic_matches("team.**.shutdown", "team.alpha.beta.shutdown") is True

    def test_single_segment_exact(self) -> None:
        assert topic_matches("status", "status") is True

    def test_single_segment_no_match(self) -> None:
        assert topic_matches("status", "progress") is False


# ═══════════════════════════════════════════════════════════════════
# InMemoryBusPersistence
# ═══════════════════════════════════════════════════════════════════


class TestInMemoryBusPersistence:
    """In-memory persistence backend operations."""

    def test_store_and_get_envelope(self) -> None:
        p = InMemoryBusPersistence()
        env = _make_envelope()
        p.store(env)
        loaded = p.get_envelope(env.envelope_id)
        assert loaded is not None
        assert loaded.envelope_id == env.envelope_id
        assert loaded.topic == env.topic

    def test_get_envelope_not_found(self) -> None:
        p = InMemoryBusPersistence()
        assert p.get_envelope("nonexistent") is None

    def test_load_pending_by_target(self) -> None:
        p = InMemoryBusPersistence()
        env = _make_envelope(target_id="bob")
        p.store(env)
        pending = p.load_pending("bob")
        assert len(pending) == 1
        assert pending[0].envelope_id == env.envelope_id

    def test_load_pending_excludes_delivered(self) -> None:
        p = InMemoryBusPersistence()
        env = _make_envelope(target_id="bob")
        p.store(env)
        p.mark_delivered(env.envelope_id)
        pending = p.load_pending("bob")
        assert len(pending) == 0

    def test_load_pending_broadcast_within_group(self) -> None:
        """Broadcast (no target) within group reaches other members, excludes sender."""
        p = InMemoryBusPersistence()
        env = _make_envelope(source_id="alice", target_id=None, group="team1")
        p.store(env)
        # bob in same group sees it
        pending = p.load_pending("bob", group="team1")
        assert len(pending) == 1
        # alice (sender) does not see it
        pending_self = p.load_pending("alice", group="team1")
        assert len(pending_self) == 0

    def test_mark_acked_advances_level(self) -> None:
        p = InMemoryBusPersistence()
        env = _make_envelope()
        p.store(env)
        assert p.get_envelope(env.envelope_id).ack_level == AckLevel.NONE
        p.mark_acked(env.envelope_id, AckLevel.RECEIVED)
        assert p.get_envelope(env.envelope_id).ack_level == AckLevel.RECEIVED
        p.mark_acked(env.envelope_id, AckLevel.HANDLED)
        assert p.get_envelope(env.envelope_id).ack_level == AckLevel.HANDLED

    def test_mark_acked_does_not_regress(self) -> None:
        p = InMemoryBusPersistence()
        env = _make_envelope()
        p.store(env)
        p.mark_acked(env.envelope_id, AckLevel.HANDLED)
        p.mark_acked(env.envelope_id, AckLevel.RECEIVED)
        assert p.get_envelope(env.envelope_id).ack_level == AckLevel.HANDLED

    def test_mark_acked_nonexistent(self) -> None:
        """Acking a nonexistent envelope is a no-op."""
        p = InMemoryBusPersistence()
        p.mark_acked("nonexistent", AckLevel.RECEIVED)

    def test_cleanup_expired(self) -> None:
        p = InMemoryBusPersistence()
        # Create expired envelope with ttl_ms=1 and old created_at
        old_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        env = BusEnvelope(
            topic="test",
            source=BusAddress(agent_id="a"),
            target=BusAddress(agent_id="b"),
            ttl_ms=1,
            created_at=old_time,
        )
        p.store(env)
        # Also add a non-expiring envelope
        env2 = _make_envelope(source_id="x", target_id="y")
        p.store(env2)
        removed = p.cleanup_expired()
        assert removed == 1
        assert p.get_envelope(env.envelope_id) is None
        assert p.get_envelope(env2.envelope_id) is not None

    def test_cleanup_group(self) -> None:
        p = InMemoryBusPersistence()
        env1 = _make_envelope(source_id="a", target_id="b", group="team_x")
        env2 = _make_envelope(source_id="c", target_id="d", group="team_y")
        p.store(env1)
        p.store(env2)
        removed = p.cleanup_group("team_x")
        assert removed == 1
        assert p.get_envelope(env1.envelope_id) is None
        assert p.get_envelope(env2.envelope_id) is not None

    def test_close_is_noop(self) -> None:
        p = InMemoryBusPersistence()
        p.close()  # Should not raise

    def test_load_pending_sorted_by_priority(self) -> None:
        p = InMemoryBusPersistence()
        env_low = _make_envelope(target_id="bob", priority=8)
        env_high = _make_envelope(target_id="bob", priority=1)
        p.store(env_low)
        p.store(env_high)
        pending = p.load_pending("bob")
        assert len(pending) == 2
        assert pending[0].priority <= pending[1].priority


# ═══════════════════════════════════════════════════════════════════
# SQLiteBusPersistence
# ═══════════════════════════════════════════════════════════════════


class TestSQLiteBusPersistence:
    """SQLite persistence backend operations + crash recovery."""

    @pytest.fixture()
    def db_path(self, tmp_path: object) -> str:
        return os.path.join(str(tmp_path), "test_bus.db")

    def test_store_and_get(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope(payload={"key": "value"})
        p.store(env)
        loaded = p.get_envelope(env.envelope_id)
        assert loaded is not None
        assert loaded.envelope_id == env.envelope_id
        assert loaded.payload == {"key": "value"}
        p.close()

    def test_get_not_found(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        assert p.get_envelope("nope") is None
        p.close()

    def test_load_pending_by_target(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope(target_id="bob")
        p.store(env)
        pending = p.load_pending("bob")
        assert len(pending) == 1
        p.close()

    def test_mark_delivered_excludes_from_pending(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope(target_id="bob")
        p.store(env)
        p.mark_delivered(env.envelope_id)
        assert len(p.load_pending("bob")) == 0
        p.close()

    def test_mark_acked_advances(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope()
        p.store(env)
        p.mark_acked(env.envelope_id, AckLevel.RECEIVED)
        assert p.get_envelope(env.envelope_id).ack_level == AckLevel.RECEIVED
        p.mark_acked(env.envelope_id, AckLevel.PROJECTED)
        assert p.get_envelope(env.envelope_id).ack_level == AckLevel.PROJECTED
        p.close()

    def test_mark_acked_no_regress(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope()
        p.store(env)
        p.mark_acked(env.envelope_id, AckLevel.HANDLED)
        p.mark_acked(env.envelope_id, AckLevel.NONE)
        assert p.get_envelope(env.envelope_id).ack_level == AckLevel.HANDLED
        p.close()

    def test_mark_acked_nonexistent(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        p.mark_acked("nonexistent", AckLevel.RECEIVED)
        p.close()

    def test_cleanup_group(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        env1 = _make_envelope(group="g1")
        env2 = _make_envelope(group="g2")
        p.store(env1)
        p.store(env2)
        removed = p.cleanup_group("g1")
        assert removed == 1
        assert p.get_envelope(env1.envelope_id) is None
        assert p.get_envelope(env2.envelope_id) is not None
        p.close()

    def test_broadcast_pending_in_group(self, db_path: str) -> None:
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope(source_id="alice", target_id=None, group="team1")
        p.store(env)
        pending = p.load_pending("bob", group="team1")
        assert len(pending) == 1
        pending_self = p.load_pending("alice", group="team1")
        assert len(pending_self) == 0
        p.close()

    def test_crash_recovery_close_reopen(self, db_path: str) -> None:
        """Data persists after close + reopen (simulating crash recovery)."""
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope(target_id="bob", payload={"data": 42})
        p.store(env)
        p.close()

        # Reopen — data should survive
        p2 = SQLiteBusPersistence(db_path)
        loaded = p2.get_envelope(env.envelope_id)
        assert loaded is not None
        assert loaded.payload == {"data": 42}
        # Pending also survives
        pending = p2.load_pending("bob")
        assert len(pending) == 1
        p2.close()

    def test_crash_recovery_delivered_state_survives(self, db_path: str) -> None:
        """Delivered state persists across close/reopen."""
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope(target_id="bob")
        p.store(env)
        p.mark_delivered(env.envelope_id)
        p.close()

        p2 = SQLiteBusPersistence(db_path)
        assert len(p2.load_pending("bob")) == 0
        assert p2.get_envelope(env.envelope_id) is not None
        p2.close()

    def test_crash_recovery_ack_level_survives(self, db_path: str) -> None:
        """Ack level persists across close/reopen."""
        p = SQLiteBusPersistence(db_path)
        env = _make_envelope()
        p.store(env)
        p.mark_acked(env.envelope_id, AckLevel.PROJECTED)
        p.close()

        p2 = SQLiteBusPersistence(db_path)
        loaded = p2.get_envelope(env.envelope_id)
        assert loaded.ack_level == AckLevel.PROJECTED
        p2.close()


# ═══════════════════════════════════════════════════════════════════
# AgentBus
# ═══════════════════════════════════════════════════════════════════


class TestAgentBus:
    """AgentBus high-level API tests."""

    def _make_bus(self) -> AgentBus:
        return AgentBus(persistence=InMemoryBusPersistence())

    # ── publish / subscribe ────────────────────────────────────

    def test_publish_triggers_subscriber(self) -> None:
        bus = self._make_bus()
        received: list[BusEnvelope] = []
        bus.subscribe("test.*", lambda env: received.append(env))
        env = _make_envelope(topic="test.hello")
        bus.publish(env)
        assert len(received) == 1
        assert received[0].envelope_id == env.envelope_id

    def test_subscribe_returns_id_and_unsubscribe(self) -> None:
        bus = self._make_bus()
        received: list[BusEnvelope] = []
        sub_id = bus.subscribe("test.*", lambda env: received.append(env))
        assert sub_id.startswith("sub_")
        bus.unsubscribe(sub_id)
        bus.publish(_make_envelope(topic="test.hello"))
        assert len(received) == 0

    def test_subscribe_filter_by_source(self) -> None:
        from agent_framework.notification.subscriber import SubscriptionFilter
        bus = self._make_bus()
        received: list[BusEnvelope] = []
        filt = SubscriptionFilter(source_agent_ids=["alice"])
        bus.subscribe("**", lambda env: received.append(env), sub_filter=filt)
        bus.publish(_make_envelope(topic="test.x", source_id="bob"))
        bus.publish(_make_envelope(topic="test.x", source_id="alice"))
        assert len(received) == 1
        assert received[0].source.agent_id == "alice"

    def test_handler_exception_does_not_propagate(self) -> None:
        bus = self._make_bus()

        def bad_handler(env: BusEnvelope) -> None:
            raise RuntimeError("boom")

        bus.subscribe("**", bad_handler)
        bus.publish(_make_envelope())  # Should not raise

    # ── drain / peek / ack ─────────────────────────────────────

    def test_drain_returns_pending_and_marks_delivered(self) -> None:
        bus = self._make_bus()
        addr_b = BusAddress(agent_id="bob", group="team1")
        env = _make_envelope(target_id="bob", group="team1")
        bus.publish(env)
        drained = bus.drain(addr_b)
        assert len(drained) == 1
        # Second drain returns empty — already delivered
        assert len(bus.drain(addr_b)) == 0

    def test_drain_with_topic_filter(self) -> None:
        bus = self._make_bus()
        addr = BusAddress(agent_id="bob", group="t")
        bus.publish(_make_envelope(topic="info.status", target_id="bob", group="t"))
        bus.publish(_make_envelope(topic="error.crash", target_id="bob", group="t"))
        drained = bus.drain(addr, topic_pattern="info.*")
        assert len(drained) == 1
        assert drained[0].topic == "info.status"

    def test_peek_does_not_mark_delivered(self) -> None:
        bus = self._make_bus()
        addr = BusAddress(agent_id="bob", group="team1")
        bus.publish(_make_envelope(target_id="bob", group="team1"))
        peeked = bus.peek(addr)
        assert len(peeked) == 1
        # Still pending after peek
        peeked2 = bus.peek(addr)
        assert len(peeked2) == 1

    def test_ack_updates_level(self) -> None:
        bus = self._make_bus()
        env = _make_envelope()
        bus.publish(env)
        bus.ack(env.envelope_id, AckLevel.RECEIVED)
        loaded = bus.get_envelope(env.envelope_id)
        assert loaded.ack_level == AckLevel.RECEIVED

    # ── broadcast / send / reply ───────────────────────────────

    def test_broadcast_creates_envelope(self) -> None:
        bus = self._make_bus()
        source = BusAddress(agent_id="alice", group="team1")
        env = bus.broadcast("notify.all", {"msg": "hello"}, source, group="team1")
        assert env.topic == "notify.all"
        assert env.target is None
        assert env.source.group == "team1"

    def test_send_point_to_point(self) -> None:
        bus = self._make_bus()
        src = BusAddress(agent_id="alice")
        tgt = BusAddress(agent_id="bob")
        env = bus.send("dm.chat", {"text": "hi"}, src, tgt)
        assert env.target.agent_id == "bob"
        assert env.source.agent_id == "alice"
        # bob can drain it
        drained = bus.drain(tgt)
        assert len(drained) == 1

    def test_reply_sets_correlation(self) -> None:
        bus = self._make_bus()
        src = BusAddress(agent_id="alice")
        tgt = BusAddress(agent_id="bob")
        original = bus.send("question", {"q": "why?"}, src, tgt)

        reply_src = BusAddress(agent_id="bob")
        reply_env = bus.reply(original, {"a": "because"}, reply_src)
        assert reply_env.correlation_id == original.envelope_id
        assert reply_env.reply_to == original.envelope_id
        assert reply_env.target.agent_id == "alice"

    # ── participants ───────────────────────────────────────────

    def test_register_and_list_participants(self) -> None:
        bus = self._make_bus()
        addr1 = BusAddress(agent_id="alice", group="team1")
        addr2 = BusAddress(agent_id="bob", group="team1")
        addr3 = BusAddress(agent_id="charlie", group="team2")
        bus.register_participant(addr1)
        bus.register_participant(addr2)
        bus.register_participant(addr3)
        all_p = bus.list_participants()
        assert len(all_p) == 3
        team1 = bus.list_participants(group="team1")
        assert len(team1) == 2
        team2 = bus.list_participants(group="team2")
        assert len(team2) == 1

    def test_unregister_participant(self) -> None:
        bus = self._make_bus()
        addr = BusAddress(agent_id="alice", group="team1")
        bus.register_participant(addr)
        assert len(bus.list_participants()) == 1
        bus.unregister_participant(addr)
        assert len(bus.list_participants()) == 0

    # ── clear_group ────────────────────────────────────────────

    def test_clear_group_removes_messages_and_participants(self) -> None:
        bus = self._make_bus()
        addr = BusAddress(agent_id="alice", group="team1")
        bus.register_participant(addr)
        bus.publish(_make_envelope(source_id="alice", target_id="bob", group="team1"))
        removed = bus.clear_group("team1")
        assert removed >= 1
        assert len(bus.list_participants(group="team1")) == 0

    # ── get_envelope ───────────────────────────────────────────

    def test_get_envelope_exists(self) -> None:
        bus = self._make_bus()
        env = _make_envelope()
        bus.publish(env)
        found = bus.get_envelope(env.envelope_id)
        assert found is not None
        assert found.envelope_id == env.envelope_id

    def test_get_envelope_not_found(self) -> None:
        bus = self._make_bus()
        assert bus.get_envelope("missing") is None

    # ── pending_count ──────────────────────────────────────────

    def test_pending_count(self) -> None:
        bus = self._make_bus()
        addr = BusAddress(agent_id="bob", group="team1")
        assert bus.pending_count(addr) == 0
        bus.publish(_make_envelope(target_id="bob", group="team1"))
        assert bus.pending_count(addr) == 1
        bus.drain(addr)
        assert bus.pending_count(addr) == 0

    # ── shutdown ───────────────────────────────────────────────

    def test_shutdown_clears_state(self) -> None:
        bus = self._make_bus()
        addr = BusAddress(agent_id="alice", group="team1")
        bus.register_participant(addr)
        bus.subscribe("**", lambda env: None)
        bus.shutdown()
        assert len(bus.list_participants()) == 0


# ═══════════════════════════════════════════════════════════════════
# E2E: 4 collaboration modes via TeamMailbox
# ═══════════════════════════════════════════════════════════════════

def _setup_team():
    """Helper: create a complete team environment for E2E tests."""
    from datetime import datetime as _dt, timezone as _tz
    from agent_framework.models.team import TeamMember, TeamMemberStatus
    from agent_framework.team.registry import TeamRegistry
    from agent_framework.team.plan_registry import PlanRegistry
    from agent_framework.team.shutdown_registry import ShutdownRegistry
    from agent_framework.team.mailbox import TeamMailbox
    from agent_framework.team.coordinator import TeamCoordinator
    from agent_framework.team.teammate_runtime import TeammateRuntime

    bus = AgentBus(persistence=InMemoryBusPersistence())
    registry = TeamRegistry("team_e2e")
    plan_reg = PlanRegistry()
    shutdown_reg = ShutdownRegistry()
    mailbox = TeamMailbox(bus, registry)

    now = _dt.now(_tz.utc)
    for aid, role in [("lead", "lead"), ("coder", "teammate"), ("reviewer", "teammate")]:
        registry.register(TeamMember(
            agent_id=aid, team_id="team_e2e", role=role,
            status=TeamMemberStatus.WORKING, joined_at=now, updated_at=now,
        ))

    coord = TeamCoordinator("team_e2e", "lead", mailbox, registry, plan_reg, shutdown_reg)
    rt_coder = TeammateRuntime("coder", "team_e2e", mailbox, registry, plan_reg)
    rt_reviewer = TeammateRuntime("reviewer", "team_e2e", mailbox, registry, plan_reg)
    return bus, mailbox, coord, rt_coder, rt_reviewer, registry, plan_reg, shutdown_reg


class TestModeAStar:
    """Mode A: Star — all communication through Lead."""

    def test_assign_and_progress(self):
        _, mailbox, coord, rt_coder, _, _, _, _ = _setup_team()
        coord.assign_task("fix parser bug", "coder")
        inbox = rt_coder.read_inbox()
        assert any(e.event_type.value == "TASK_ASSIGNMENT" for e in inbox)

        rt_coder.report_progress(50, "halfway")
        lead_inbox = mailbox.read_inbox("lead")
        assert any(e.payload.get("percent") == 50 for e in lead_inbox)

    def test_question_answer_flow(self):
        _, mailbox, coord, rt_coder, _, _, _, _ = _setup_team()
        req_id = rt_coder.ask_question("which module?", ["parser", "lexer"])

        lead_inbox = mailbox.read_inbox("lead")
        questions = [e for e in lead_inbox if e.event_type.value == "QUESTION"]
        assert len(questions) == 1

        coord.answer_question(req_id, "parser module", "coder")
        coder_inbox = rt_coder.read_inbox()
        answers = [e for e in coder_inbox if e.event_type.value == "ANSWER"]
        assert len(answers) == 1
        assert answers[0].payload["answer"] == "parser module"


class TestModeBMesh:
    """Mode B: Mesh — teammates communicate directly."""

    def test_sibling_direct_message(self):
        _, _, _, rt_coder, rt_reviewer, _, _, _ = _setup_team()
        rt_coder.send_to_sibling("reviewer", "please review my PR")
        inbox = rt_reviewer.read_inbox()
        assert any("review" in e.payload.get("message", "") for e in inbox)

    def test_broadcast(self):
        _, mailbox, _, rt_coder, rt_reviewer, _, _, _ = _setup_team()
        from agent_framework.models.team import MailEvent, MailEventType
        mailbox.broadcast(MailEvent(
            team_id="team_e2e", from_agent="coder", to_agent="*",
            event_type=MailEventType.BROADCAST_NOTICE,
            payload={"message": "critical bug found"},
        ))
        reviewer_inbox = rt_reviewer.read_inbox()
        lead_inbox = mailbox.read_inbox("lead")
        assert any("critical" in e.payload.get("message", "") for e in reviewer_inbox)
        assert any("critical" in e.payload.get("message", "") for e in lead_inbox)


class TestModeCPubSub:
    """Mode C: Publish/Subscribe — topic-driven."""

    def test_publish_subscribe(self):
        _, mailbox, _, rt_coder, rt_reviewer, _, _, _ = _setup_team()
        mailbox.subscribe("reviewer", "findings.*")
        mailbox.publish("findings.security", {"vuln": "XSS"}, "coder", "team_e2e")

        inbox = rt_reviewer.read_inbox()
        assert any(e.payload.get("vuln") == "XSS" for e in inbox)

    def test_unsubscribe_stops_delivery(self):
        _, mailbox, _, rt_coder, rt_reviewer, _, _, _ = _setup_team()
        mailbox.subscribe("reviewer", "findings.*")
        mailbox.unsubscribe("reviewer", "findings.*")
        mailbox.publish("findings.security", {"vuln": "SQLi"}, "coder", "team_e2e")

        inbox = rt_reviewer.read_inbox()
        assert not any(e.payload.get("vuln") == "SQLi" for e in inbox)


class TestModeDRequestReply:
    """Mode D: Request/Reply with correlation."""

    def test_request_reply_chain(self):
        _, mailbox, _, rt_coder, rt_reviewer, _, _, _ = _setup_team()
        from agent_framework.models.team import MailEvent, MailEventType

        request = mailbox.send(MailEvent(
            team_id="team_e2e", from_agent="coder", to_agent="reviewer",
            event_type=MailEventType.QUESTION,
            payload={"request_id": "q1", "question": "refactor ok?"},
        ))

        reviewer_inbox = rt_reviewer.read_inbox()
        assert len(reviewer_inbox) >= 1

        reply = mailbox.reply(
            reviewer_inbox[0].event_id,
            {"answer": "approved"},
            source="reviewer",
        )

        coder_inbox = rt_coder.read_inbox()
        replies = [e for e in coder_inbox if e.correlation_id]
        assert len(replies) == 1
        assert replies[0].payload["answer"] == "approved"


class TestShutdownHandshake:
    """Graceful shutdown flow: Lead → request → Worker ACK → complete."""

    def test_full_shutdown(self):
        from agent_framework.models.team import TeamMemberStatus
        _, mailbox, coord, rt_coder, _, registry, _, _ = _setup_team()

        req_id = coord.shutdown_teammate("coder", "done")
        inbox = rt_coder.read_inbox()
        shutdown_events = [e for e in inbox if e.event_type.value == "SHUTDOWN_REQUEST"]
        assert len(shutdown_events) == 1

        rt_coder.handle_event(shutdown_events[0])
        coord.process_inbox()

        member = registry.get("coder")
        assert member.status == TeamMemberStatus.SHUTDOWN


class TestPlanApproval:
    """Plan submission → Lead review → approval/rejection."""

    def test_approve(self):
        _, mailbox, coord, rt_coder, _, _, plan_reg, _ = _setup_team()
        req_id = rt_coder.submit_plan("Refactor", "Split module", risk_level="medium")

        lead_inbox = mailbox.read_inbox("lead")
        assert any(e.event_type.value == "PLAN_SUBMISSION" for e in lead_inbox)

        coord.approve_plan(req_id, "good plan")
        coder_inbox = rt_coder.read_inbox()
        approvals = [e for e in coder_inbox if e.event_type.value == "APPROVAL_RESPONSE"]
        assert len(approvals) == 1
        assert approvals[0].payload["approved"] is True

    def test_reject(self):
        _, mailbox, coord, rt_coder, _, _, _, _ = _setup_team()
        req_id = rt_coder.submit_plan("Risky change", "Delete everything", risk_level="high")
        mailbox.read_inbox("lead")  # consume

        coord.reject_plan(req_id, "too risky")
        coder_inbox = rt_coder.read_inbox()
        rejections = [e for e in coder_inbox if e.event_type.value == "APPROVAL_RESPONSE"]
        assert len(rejections) == 1
        assert rejections[0].payload["approved"] is False

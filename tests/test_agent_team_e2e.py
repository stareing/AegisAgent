"""E2E tests for 4 Agent Team collaboration modes.

Mode A (Star): Lead spawns teammates, assigns tasks, receives progress/question, answers, collects
Mode B (Mesh): Teammates send messages directly to siblings, Lead observes via read
Mode C (Pub/Sub): Agent publishes to topic, subscribers auto-receive
Mode D (Request/Reply): Agent sends with event_id, other replies with correlation
"""

from __future__ import annotations

import pytest

from agent_framework.models.team import (
    MailEvent,
    MailEventType,
    TeamMember,
    TeamMemberStatus,
)
from agent_framework.notification.bus import AgentBus
from agent_framework.notification.envelope import BusAddress
from agent_framework.notification.persistence import InMemoryBusPersistence
from agent_framework.team.coordinator import TeamCoordinator
from agent_framework.team.mailbox import TeamMailbox
from agent_framework.team.plan_registry import PlanRegistry
from agent_framework.team.registry import TeamRegistry
from agent_framework.team.shutdown_registry import ShutdownRegistry
from agent_framework.team.teammate_runtime import TeammateRuntime


# ═══════════════════════════════════════════════════════════════════
# Fixtures — full team stack
# ═══════════════════════════════════════════════════════════════════

TEAM_ID = "e2e_team"
LEAD_ID = "orchestrator_e2e"  # Non-default: verifies no hardcoded "lead"


@pytest.fixture()
def team_stack():
    """Build a complete team stack: bus, registry, mailbox, coordinator, and helper to spawn teammates."""
    bus = AgentBus(persistence=InMemoryBusPersistence())
    registry = TeamRegistry(team_id=TEAM_ID)
    plan_registry = PlanRegistry()
    shutdown_registry = ShutdownRegistry()
    mailbox = TeamMailbox(bus=bus, team_registry=registry)

    coordinator = TeamCoordinator(
        team_id=TEAM_ID,
        lead_agent_id=LEAD_ID,
        mailbox=mailbox,
        team_registry=registry,
        plan_registry=plan_registry,
        shutdown_registry=shutdown_registry,
    )
    coordinator.create_team(name="E2E Test Team")

    def make_teammate(agent_id: str, role: str = "worker") -> TeammateRuntime:
        member = TeamMember(
            agent_id=agent_id,
            team_id=TEAM_ID,
            role=role,
            status=TeamMemberStatus.WORKING,
        )
        registry.register(member)
        return TeammateRuntime(
            agent_id=agent_id,
            team_id=TEAM_ID,
            mailbox=mailbox,
            team_registry=registry,
            plan_registry=plan_registry,
        )

    return {
        "bus": bus,
        "registry": registry,
        "plan_registry": plan_registry,
        "shutdown_registry": shutdown_registry,
        "mailbox": mailbox,
        "coordinator": coordinator,
        "make_teammate": make_teammate,
    }


# ═══════════════════════════════════════════════════════════════════
# Mode A — Star: Lead ↔ Teammates
# ═══════════════════════════════════════════════════════════════════


class TestModeAStar:
    """Lead spawns teammates, assigns tasks, processes Q&A, collects results."""

    def test_lead_assigns_task_teammate_receives(self, team_stack: dict) -> None:
        coordinator = team_stack["coordinator"]
        make_tm = team_stack["make_teammate"]

        tm = make_tm("tm_alpha", role="coder")
        coordinator.assign_task("Write unit tests", "tm_alpha")

        inbox = tm.read_inbox()
        assert len(inbox) == 1
        assert inbox[0].event_type == MailEventType.TASK_ASSIGNMENT
        assert inbox[0].payload["task"] == "Write unit tests"

    def test_teammate_sends_progress_lead_receives(self, team_stack: dict) -> None:
        coordinator = team_stack["coordinator"]
        make_tm = team_stack["make_teammate"]

        tm = make_tm("tm_beta", role="tester")
        tm.report_progress(percent=50, summary="Halfway done")

        results = coordinator.process_inbox()
        assert len(results) == 1
        assert results[0]["type"] == "progress"
        assert results[0]["payload"]["percent"] == 50

    def test_teammate_asks_question_lead_answers(self, team_stack: dict) -> None:
        coordinator = team_stack["coordinator"]
        make_tm = team_stack["make_teammate"]

        tm = make_tm("tm_gamma")
        request_id = tm.ask_question("What framework to use?", options=["pytest", "unittest"])

        # Lead sees the question
        results = coordinator.process_inbox()
        assert len(results) == 1
        assert results[0]["type"] == "question"
        assert results[0]["question"] == "What framework to use?"

        # Lead answers
        coordinator.answer_question(request_id, "Use pytest", to_agent="tm_gamma")

        # Teammate receives answer
        inbox = tm.read_inbox()
        assert len(inbox) == 1
        result = tm.handle_event(inbox[0])
        assert result["type"] == "answer"
        assert result["answer"] == "Use pytest"

    def test_teammate_submits_plan_lead_approves(self, team_stack: dict) -> None:
        coordinator = team_stack["coordinator"]
        make_tm = team_stack["make_teammate"]

        tm = make_tm("tm_delta")
        plan_id = tm.submit_plan(
            title="Refactor DB layer",
            plan_text="Replace raw SQL with ORM",
            risk_level="high",
        )

        # Lead processes plan
        results = coordinator.process_inbox()
        assert len(results) == 1
        assert results[0]["type"] == "plan_submission"
        assert results[0]["risk_level"] == "high"

        # Lead approves
        coordinator.approve_plan(plan_id, feedback="Go ahead")

        # Teammate sees approval
        inbox = tm.read_inbox()
        assert len(inbox) == 1
        result = tm.handle_event(inbox[0])
        assert result["type"] == "approval"
        assert result["approved"] is True
        assert result["feedback"] == "Go ahead"

    def test_lead_shuts_down_teammate(self, team_stack: dict) -> None:
        coordinator = team_stack["coordinator"]
        registry = team_stack["registry"]
        make_tm = team_stack["make_teammate"]

        tm = make_tm("tm_epsilon")
        coordinator.shutdown_teammate("tm_epsilon", reason="task complete")

        # Teammate processes shutdown request — sets itself to SHUTDOWN
        inbox = tm.read_inbox()
        assert len(inbox) == 1
        result = tm.handle_event(inbox[0])
        assert result["type"] == "shutdown"

        # Teammate is now in terminal SHUTDOWN status
        member = registry.get("tm_epsilon")
        assert member.status == TeamMemberStatus.SHUTDOWN

    def test_full_star_lifecycle(self, team_stack: dict) -> None:
        """Complete Star flow: assign → progress → question → answer → shutdown."""
        coordinator = team_stack["coordinator"]
        make_tm = team_stack["make_teammate"]
        registry = team_stack["registry"]

        tm = make_tm("tm_full")

        # 1. Assign task
        coordinator.assign_task("Build API", "tm_full")
        inbox = tm.read_inbox()
        assert len(inbox) == 1
        tm.handle_event(inbox[0])

        # 2. Progress
        tm.report_progress(30, "API routes defined")
        results = coordinator.process_inbox()
        assert results[0]["type"] == "progress"

        # 3. Question
        qid = tm.ask_question("REST or GraphQL?")
        coordinator.process_inbox()
        coordinator.answer_question(qid, "REST", to_agent="tm_full")
        inbox = tm.read_inbox()
        tm.handle_event(inbox[0])

        # 4. Complete + shutdown
        tm.report_progress(100, "Done")
        coordinator.process_inbox()
        coordinator.shutdown_teammate("tm_full")
        inbox = tm.read_inbox()
        tm.handle_event(inbox[0])

        assert registry.get("tm_full").status == TeamMemberStatus.SHUTDOWN


# ═══════════════════════════════════════════════════════════════════
# Mode B — Mesh: Teammates ↔ Teammates directly
# ═══════════════════════════════════════════════════════════════════


class TestModeBMesh:
    """Teammates send messages directly to siblings. Lead observes."""

    def test_sibling_direct_message(self, team_stack: dict) -> None:
        make_tm = team_stack["make_teammate"]

        tm_a = make_tm("tm_mesh_a", role="frontend")
        tm_b = make_tm("tm_mesh_b", role="backend")

        tm_a.send_to_sibling("tm_mesh_b", "API contract ready", {"schema_version": "2.0"})

        inbox = tm_b.read_inbox()
        assert len(inbox) == 1
        assert inbox[0].event_type == MailEventType.BROADCAST_NOTICE
        assert inbox[0].payload["message"] == "API contract ready"
        assert inbox[0].payload["schema_version"] == "2.0"

    def test_bidirectional_sibling_exchange(self, team_stack: dict) -> None:
        make_tm = team_stack["make_teammate"]

        tm_a = make_tm("tm_mesh_c", role="designer")
        tm_b = make_tm("tm_mesh_d", role="dev")

        # A → B
        tm_a.send_to_sibling("tm_mesh_d", "Design spec sent")
        inbox_b = tm_b.read_inbox()
        assert len(inbox_b) == 1

        # B → A
        tm_b.send_to_sibling("tm_mesh_c", "Design received, starting impl")
        inbox_a = tm_a.read_inbox()
        assert len(inbox_a) == 1
        assert inbox_a[0].payload["message"] == "Design received, starting impl"

    def test_lead_can_observe_via_read(self, team_stack: dict) -> None:
        """Lead can read messages that are addressed to the lead."""
        coordinator = team_stack["coordinator"]
        make_tm = team_stack["make_teammate"]

        tm = make_tm("tm_mesh_e")
        # Teammate sends progress to lead — lead reads it
        tm.report_progress(80, "Almost done")
        results = coordinator.process_inbox()
        assert len(results) == 1
        assert results[0]["type"] == "progress"


# ═══════════════════════════════════════════════════════════════════
# Mode C — Pub/Sub: Topic-based publish, subscribers auto-receive
# ═══════════════════════════════════════════════════════════════════


class TestModeCPubSub:
    """Agent publishes to topic, subscribers auto-receive."""

    def test_publish_to_topic_with_subscribers(self, team_stack: dict) -> None:
        mailbox = team_stack["mailbox"]
        make_tm = team_stack["make_teammate"]

        tm_a = make_tm("tm_pub_a")
        tm_b = make_tm("tm_pub_b")
        tm_c = make_tm("tm_pub_c")

        # B and C subscribe to "status.update"
        mailbox.subscribe("tm_pub_b", "status.update")
        mailbox.subscribe("tm_pub_c", "status.update")

        # A publishes
        sent = mailbox.publish(
            topic="status.update",
            payload={"component": "db", "healthy": True},
            source="tm_pub_a",
            team_id=TEAM_ID,
        )

        assert len(sent) == 2  # B and C received

        inbox_b = tm_b.read_inbox()
        assert len(inbox_b) == 1
        assert inbox_b[0].payload["component"] == "db"
        assert inbox_b[0].payload["_topic"] == "status.update"

        inbox_c = tm_c.read_inbox()
        assert len(inbox_c) == 1

    def test_publish_excludes_sender(self, team_stack: dict) -> None:
        mailbox = team_stack["mailbox"]
        make_tm = team_stack["make_teammate"]

        tm_a = make_tm("tm_pub_self")

        # Subscribe self
        mailbox.subscribe("tm_pub_self", "updates.**")

        # Publish from self — should NOT receive own message
        sent = mailbox.publish(
            topic="updates.test",
            payload={"x": 1},
            source="tm_pub_self",
        )
        assert len(sent) == 0

    def test_unsubscribe_stops_delivery(self, team_stack: dict) -> None:
        mailbox = team_stack["mailbox"]
        make_tm = team_stack["make_teammate"]

        tm = make_tm("tm_unsub")
        mailbox.subscribe("tm_unsub", "alerts.*")

        # Unsubscribe
        mailbox.unsubscribe("tm_unsub", "alerts.*")

        sent = mailbox.publish(topic="alerts.fire", payload={}, source="other")
        assert len(sent) == 0

    def test_multiple_topics_separate_subscribers(self, team_stack: dict) -> None:
        mailbox = team_stack["mailbox"]
        make_tm = team_stack["make_teammate"]

        tm_x = make_tm("tm_topicx")
        tm_y = make_tm("tm_topicy")

        mailbox.subscribe("tm_topicx", "deploy.*")
        mailbox.subscribe("tm_topicy", "monitor.*")

        mailbox.publish(topic="deploy.staging", payload={"env": "staging"}, source="ci")
        mailbox.publish(topic="monitor.cpu", payload={"usage": 80}, source="mon")

        inbox_x = tm_x.read_inbox()
        assert len(inbox_x) == 1
        assert inbox_x[0].payload["env"] == "staging"

        inbox_y = tm_y.read_inbox()
        assert len(inbox_y) == 1
        assert inbox_y[0].payload["usage"] == 80


# ═══════════════════════════════════════════════════════════════════
# Mode D — Request/Reply: correlated event exchange
# ═══════════════════════════════════════════════════════════════════


class TestModeDRequestReply:
    """Agent sends with event_id, other replies with correlation."""

    def test_request_reply_correlation(self, team_stack: dict) -> None:
        mailbox = team_stack["mailbox"]
        make_tm = team_stack["make_teammate"]

        tm_req = make_tm("tm_requester")
        tm_resp = make_tm("tm_responder")

        # Requester sends a question event
        request_event = MailEvent(
            team_id=TEAM_ID,
            from_agent="tm_requester",
            to_agent="tm_responder",
            event_type=MailEventType.QUESTION,
            request_id="req_001",
            payload={
                "request_id": "req_001",
                "question": "What is the status?",
            },
        )
        sent = mailbox.send(request_event)
        original_event_id = sent.event_id

        # Responder reads inbox
        inbox = tm_resp.read_inbox()
        assert len(inbox) == 1
        assert inbox[0].payload["question"] == "What is the status?"

        # Responder replies using mailbox.reply with correlation
        reply_event = mailbox.reply(
            original_event_id=original_event_id,
            payload={"request_id": "req_001", "answer": "All systems go"},
            source="tm_responder",
        )

        # Requester reads the reply
        inbox_req = tm_req.read_inbox()
        assert len(inbox_req) == 1
        assert inbox_req[0].correlation_id == original_event_id
        assert inbox_req[0].payload["answer"] == "All systems go"

    def test_multi_round_request_reply(self, team_stack: dict) -> None:
        """Multiple request-reply rounds with proper correlation."""
        mailbox = team_stack["mailbox"]
        make_tm = team_stack["make_teammate"]

        tm_a = make_tm("tm_rr_a")
        tm_b = make_tm("tm_rr_b")

        # Round 1: A asks B
        q1 = mailbox.send(MailEvent(
            team_id=TEAM_ID,
            from_agent="tm_rr_a",
            to_agent="tm_rr_b",
            event_type=MailEventType.QUESTION,
            request_id="rr_q1",
            payload={"request_id": "rr_q1", "question": "Q1?"},
        ))
        q1_id = q1.event_id

        inbox_b = tm_b.read_inbox()
        assert len(inbox_b) == 1

        reply1 = mailbox.reply(q1_id, {"request_id": "rr_q1", "answer": "A1"}, "tm_rr_b")

        inbox_a = tm_a.read_inbox()
        assert len(inbox_a) == 1
        assert inbox_a[0].correlation_id == q1_id

        # Round 2: A asks B again
        q2 = mailbox.send(MailEvent(
            team_id=TEAM_ID,
            from_agent="tm_rr_a",
            to_agent="tm_rr_b",
            event_type=MailEventType.QUESTION,
            request_id="rr_q2",
            payload={"request_id": "rr_q2", "question": "Q2?"},
        ))
        q2_id = q2.event_id

        inbox_b2 = tm_b.read_inbox()
        assert len(inbox_b2) == 1
        reply2 = mailbox.reply(q2_id, {"request_id": "rr_q2", "answer": "A2"}, "tm_rr_b")

        inbox_a2 = tm_a.read_inbox()
        assert len(inbox_a2) == 1
        assert inbox_a2[0].correlation_id == q2_id
        assert inbox_a2[0].payload["answer"] == "A2"

    def test_reply_to_nonexistent_raises(self, team_stack: dict) -> None:
        mailbox = team_stack["mailbox"]
        with pytest.raises(ValueError, match="not found"):
            mailbox.reply("nonexistent_id", {"answer": "x"}, "agent")


# ═══════════════════════════════════════════════════════════════════
# Cross-mode integration
# ═══════════════════════════════════════════════════════════════════


class TestCrossModeIntegration:
    """Tests combining multiple collaboration modes in a single team."""

    def test_star_plus_mesh_combined(self, team_stack: dict) -> None:
        """Lead assigns tasks (Star), teammates collaborate (Mesh)."""
        coordinator = team_stack["coordinator"]
        make_tm = team_stack["make_teammate"]

        tm_fe = make_tm("tm_cross_fe", role="frontend")
        tm_be = make_tm("tm_cross_be", role="backend")

        # Star: Lead assigns
        coordinator.assign_task("Build login page", "tm_cross_fe")
        coordinator.assign_task("Build auth API", "tm_cross_be")

        # Teammates receive assignments
        fe_inbox = tm_fe.read_inbox()
        be_inbox = tm_be.read_inbox()
        assert len(fe_inbox) == 1
        assert len(be_inbox) == 1

        # Mesh: Backend tells frontend about API contract
        tm_be.send_to_sibling("tm_cross_fe", "Auth API ready at /api/auth")
        fe_inbox2 = tm_fe.read_inbox()
        assert len(fe_inbox2) == 1
        assert fe_inbox2[0].payload["message"] == "Auth API ready at /api/auth"

        # Star: Both report progress
        tm_fe.report_progress(100, "Login page done")
        tm_be.report_progress(100, "Auth API done")
        results = coordinator.process_inbox()
        assert len(results) == 2

    def test_pubsub_plus_request_reply(self, team_stack: dict) -> None:
        """Pub/Sub for broadcast updates + Request/Reply for targeted Q&A."""
        mailbox = team_stack["mailbox"]
        make_tm = team_stack["make_teammate"]

        tm_ops = make_tm("tm_ops")
        tm_dev = make_tm("tm_dev")
        tm_qa = make_tm("tm_qa")

        # Pub/Sub: ops broadcasts status
        mailbox.subscribe("tm_dev", "infra.*")
        mailbox.subscribe("tm_qa", "infra.*")
        mailbox.publish(topic="infra.deploy", payload={"version": "2.1"}, source="tm_ops")

        dev_inbox = tm_dev.read_inbox()
        qa_inbox = tm_qa.read_inbox()
        assert len(dev_inbox) == 1
        assert len(qa_inbox) == 1

        # Request/Reply: dev asks qa
        q = mailbox.send(MailEvent(
            team_id=TEAM_ID,
            from_agent="tm_dev",
            to_agent="tm_qa",
            event_type=MailEventType.QUESTION,
            request_id="cross_q1",
            payload={"request_id": "cross_q1", "question": "Tests pass?"},
        ))
        qa_inbox2 = tm_qa.read_inbox()
        assert len(qa_inbox2) == 1

        mailbox.reply(q.event_id, {"request_id": "cross_q1", "answer": "All green"}, "tm_qa")
        dev_inbox2 = tm_dev.read_inbox()
        assert len(dev_inbox2) == 1
        assert dev_inbox2[0].payload["answer"] == "All green"

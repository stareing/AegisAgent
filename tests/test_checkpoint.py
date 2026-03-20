"""Tests for checkpoint store and resume flow."""

import pytest


class TestSQLiteCheckpointStore:

    def _make_store(self, tmp_path):
        from agent_framework.subagent.checkpoint import SQLiteCheckpointStore
        return SQLiteCheckpointStore(db_path=str(tmp_path / "ckpt.db"))

    def _make_agent_state(self, task="test task", iteration_count=3):
        from agent_framework.models.agent import AgentState
        return AgentState(run_id="run1", task=task, iteration_count=iteration_count)

    def _make_session_state(self):
        from agent_framework.models.session import SessionState
        from agent_framework.models.message import Message
        ss = SessionState(session_id="sess1", run_id="run1")
        ss.append_message(Message(role="user", content="hello"))
        ss.append_message(Message(role="assistant", content="hi there"))
        return ss

    def test_save_and_load(self, tmp_path):
        store = self._make_store(tmp_path)
        agent_state = self._make_agent_state()
        session_state = self._make_session_state()

        ckpt_id = store.save("sp1", agent_state, session_state, summary="mid-run")
        assert ckpt_id.startswith("ckpt_")

        loaded = store.load_latest("sp1")
        assert loaded is not None
        assert loaded.checkpoint_id == ckpt_id
        assert loaded.spawn_id == "sp1"
        assert loaded.iteration_index == 3
        assert loaded.summary == "mid-run"
        store.close()

    def test_restore_state(self, tmp_path):
        store = self._make_store(tmp_path)
        agent_state = self._make_agent_state(task="important task", iteration_count=5)
        session_state = self._make_session_state()

        store.save("sp1", agent_state, session_state)
        loaded = store.load_latest("sp1")

        restored_agent = loaded.restore_agent_state()
        assert restored_agent.task == "important task"
        assert restored_agent.iteration_count == 5

        restored_session = loaded.restore_session_state()
        msgs = restored_session.get_messages()
        assert len(msgs) == 2
        assert msgs[0].content == "hello"
        assert msgs[1].content == "hi there"
        store.close()

    def test_load_by_id(self, tmp_path):
        store = self._make_store(tmp_path)
        ckpt_id = store.save("sp1", self._make_agent_state(), self._make_session_state())
        loaded = store.load_by_id(ckpt_id)
        assert loaded is not None
        assert loaded.checkpoint_id == ckpt_id
        store.close()

    def test_load_latest_returns_newest(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save("sp1", self._make_agent_state(iteration_count=1), self._make_session_state(), summary="first")
        store.save("sp1", self._make_agent_state(iteration_count=5), self._make_session_state(), summary="second")

        loaded = store.load_latest("sp1")
        assert loaded.summary == "second"
        assert loaded.iteration_index == 5
        store.close()

    def test_load_missing_returns_none(self, tmp_path):
        store = self._make_store(tmp_path)
        assert store.load_latest("nonexistent") is None
        assert store.load_by_id("nonexistent") is None
        store.close()

    def test_list_checkpoints(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save("sp1", self._make_agent_state(iteration_count=1), self._make_session_state())
        store.save("sp1", self._make_agent_state(iteration_count=3), self._make_session_state())
        store.save("sp2", self._make_agent_state(iteration_count=2), self._make_session_state())

        sp1_list = store.list_checkpoints("sp1")
        assert len(sp1_list) == 2
        # Ordered by created_at DESC
        assert sp1_list[0]["iteration_index"] == 3

        sp2_list = store.list_checkpoints("sp2")
        assert len(sp2_list) == 1
        store.close()

    def test_delete(self, tmp_path):
        store = self._make_store(tmp_path)
        ckpt_id = store.save("sp1", self._make_agent_state(), self._make_session_state())
        assert store.delete(ckpt_id)
        assert store.load_by_id(ckpt_id) is None
        assert not store.delete("nonexistent")
        store.close()

    def test_delete_for_spawn(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save("sp1", self._make_agent_state(), self._make_session_state())
        store.save("sp1", self._make_agent_state(), self._make_session_state())
        store.save("sp2", self._make_agent_state(), self._make_session_state())

        deleted = store.delete_for_spawn("sp1")
        assert deleted == 2
        assert store.load_latest("sp1") is None
        assert store.load_latest("sp2") is not None
        store.close()

    def test_persistence_across_reopen(self, tmp_path):
        from agent_framework.subagent.checkpoint import SQLiteCheckpointStore
        db = str(tmp_path / "persist.db")
        s1 = SQLiteCheckpointStore(db_path=db)
        s1.save("sp1", self._make_agent_state(), self._make_session_state(), summary="saved")
        s1.close()

        s2 = SQLiteCheckpointStore(db_path=db)
        loaded = s2.load_latest("sp1")
        assert loaded is not None
        assert loaded.summary == "saved"
        s2.close()
"""Tests for sibling communication channel."""


class TestSiblingChannel:

    def _make_channel(self):
        from agent_framework.subagent.sibling_channel import SiblingChannel
        return SiblingChannel()

    def test_send_and_receive(self):
        ch = self._make_channel()
        msg = ch.send("agentA", "agentB", "run1", "hello from A")
        assert msg.message_id.startswith("sib_")
        assert msg.from_spawn_id == "agentA"
        assert msg.to_spawn_id == "agentB"

        received = ch.receive("agentB", "run1")
        assert len(received) == 1
        assert received[0].content == "hello from A"
        assert received[0].read is True

    def test_receive_marks_read(self):
        ch = self._make_channel()
        ch.send("A", "B", "run1", "msg1")
        ch.send("A", "B", "run1", "msg2")

        first = ch.receive("B", "run1")
        assert len(first) == 2

        # Second receive returns empty (already read)
        second = ch.receive("B", "run1")
        assert len(second) == 0

    def test_peek_does_not_mark_read(self):
        ch = self._make_channel()
        ch.send("A", "B", "run1", "msg1")

        peeked = ch.peek("B", "run1")
        assert len(peeked) == 1

        # Still unread
        received = ch.receive("B", "run1")
        assert len(received) == 1

    def test_cross_run_isolation(self):
        ch = self._make_channel()
        ch.send("A", "B", "run1", "msg for run1")
        ch.send("A", "B", "run2", "msg for run2")

        r1 = ch.receive("B", "run1")
        assert len(r1) == 1
        assert r1[0].content == "msg for run1"

        r2 = ch.receive("B", "run2")
        assert len(r2) == 1
        assert r2[0].content == "msg for run2"

    def test_unread_count(self):
        ch = self._make_channel()
        ch.send("A", "B", "run1", "m1")
        ch.send("A", "B", "run1", "m2")
        assert ch.unread_count("B", "run1") == 2

        ch.receive("B", "run1")
        assert ch.unread_count("B", "run1") == 0

    def test_list_siblings(self):
        ch = self._make_channel()
        ch.send("A", "B", "run1", "hi")
        ch.send("A", "C", "run1", "hi")
        siblings = ch.list_siblings("run1")
        assert sorted(siblings) == ["B", "C"]

    def test_clear_run(self):
        ch = self._make_channel()
        ch.send("A", "B", "run1", "m1")
        ch.send("A", "C", "run1", "m2")
        ch.send("A", "B", "run2", "m3")

        cleared = ch.clear_run("run1")
        assert cleared == 2  # Two mailboxes for run1
        assert ch.receive("B", "run1") == []
        assert len(ch.receive("B", "run2")) == 1

    def test_mailbox_overflow_drops_oldest(self):
        from agent_framework.subagent.sibling_channel import SiblingChannel
        ch = SiblingChannel(max_messages_per_pair=3)
        for i in range(5):
            ch.send("A", "B", "run1", f"msg{i}")

        msgs = ch.peek("B", "run1")
        assert len(msgs) == 3
        # Oldest dropped
        assert msgs[0].content == "msg2"
        assert msgs[2].content == "msg4"

    def test_payload_preserved(self):
        ch = self._make_channel()
        ch.send("A", "B", "run1", "with data", payload={"key": "value"})
        received = ch.receive("B", "run1")
        assert received[0].payload == {"key": "value"}

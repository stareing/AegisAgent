"""Strict unit tests for memory layer.

Covers:
- SQLiteMemoryStore CRUD operations
- BaseMemoryManager governance (remember, forget, pin, activate, etc.)
- DefaultMemoryManager pattern extraction and merge rules
- MemoryScope managers (Isolated, InheritRead, SharedWrite) with snapshot semantics
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent_framework.memory.sqlite_store import SQLiteMemoryStore
from agent_framework.memory.base_manager import BaseMemoryManager
from agent_framework.memory.default_manager import DefaultMemoryManager
from agent_framework.models.memory import (
    MemoryCandidate,
    MemoryKind,
    MemoryRecord,
    MemoryUpdateAction,
)
from agent_framework.subagent.memory_scope import (
    InheritReadMemoryManager,
    IsolatedMemoryManager,
    SharedWriteMemoryManager,
)


# =====================================================================
# Helpers
# =====================================================================

def _make_record(
    memory_id: str | None = None,
    agent_id: str = "agent_1",
    user_id: str | None = "user_1",
    kind: MemoryKind = MemoryKind.CUSTOM,
    title: str = "Test",
    content: str = "Test content",
    tags: list[str] | None = None,
    is_active: bool = True,
    is_pinned: bool = False,
) -> MemoryRecord:
    return MemoryRecord(
        memory_id=memory_id or str(uuid.uuid4()),
        agent_id=agent_id,
        user_id=user_id,
        kind=kind,
        title=title,
        content=content,
        tags=tags or [],
        is_active=is_active,
        is_pinned=is_pinned,
    )


def _make_candidate(
    kind: MemoryKind = MemoryKind.USER_PREFERENCE,
    title: str = "Test",
    content: str = "pref content",
    tags: list[str] | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        kind=kind, title=title, content=content, tags=tags or ["test"]
    )


def _make_agent_state(task: str = "do something"):
    from agent_framework.models.agent import AgentState
    return AgentState(run_id="run_1", task=task)


# =====================================================================
# SQLiteMemoryStore
# =====================================================================


class TestSQLiteMemoryStore:
    def setup_method(self):
        self.store = SQLiteMemoryStore(db_path=":memory:")

    def teardown_method(self):
        self.store.close()

    def test_save_and_get(self):
        rec = _make_record(memory_id="m1")
        result_id = self.store.save(rec)
        assert result_id == "m1"
        fetched = self.store.get("m1")
        assert fetched is not None
        assert fetched.memory_id == "m1"
        assert fetched.title == rec.title
        assert fetched.content == rec.content

    def test_get_nonexistent_returns_none(self):
        assert self.store.get("nonexistent") is None

    def test_update(self):
        rec = _make_record(memory_id="m1", content="v1")
        self.store.save(rec)
        rec.content = "v2"
        rec.version = 2
        self.store.update(rec)
        fetched = self.store.get("m1")
        assert fetched.content == "v2"
        assert fetched.version == 2

    def test_delete(self):
        rec = _make_record(memory_id="m1")
        self.store.save(rec)
        self.store.delete("m1")
        assert self.store.get("m1") is None

    def test_list_by_user_active_only(self):
        self.store.save(_make_record(memory_id="a", is_active=True))
        self.store.save(_make_record(memory_id="b", is_active=False))
        results = self.store.list_by_user("agent_1", "user_1", active_only=True)
        assert len(results) == 1
        assert results[0].memory_id == "a"

    def test_list_by_user_all(self):
        self.store.save(_make_record(memory_id="a", is_active=True))
        self.store.save(_make_record(memory_id="b", is_active=False))
        results = self.store.list_by_user("agent_1", "user_1", active_only=False)
        assert len(results) == 2

    def test_list_by_kind(self):
        self.store.save(_make_record(memory_id="a", kind=MemoryKind.USER_PREFERENCE))
        self.store.save(_make_record(memory_id="b", kind=MemoryKind.PROJECT_CONTEXT))
        results = self.store.list_by_kind("agent_1", "user_1", MemoryKind.USER_PREFERENCE)
        assert len(results) == 1
        assert results[0].kind == MemoryKind.USER_PREFERENCE

    def test_list_recent(self):
        for i in range(5):
            self.store.save(_make_record(memory_id=f"m{i}"))
        results = self.store.list_recent("agent_1", "user_1", limit=3)
        assert len(results) == 3

    def test_touch_increments_use_count(self):
        self.store.save(_make_record(memory_id="m1"))
        self.store.touch("m1")
        self.store.touch("m1")
        fetched = self.store.get("m1")
        assert fetched.use_count == 2
        assert fetched.last_used_at is not None

    def test_count(self):
        self.store.save(_make_record(memory_id="a"))
        self.store.save(_make_record(memory_id="b"))
        assert self.store.count("agent_1", "user_1") == 2

    def test_count_empty(self):
        assert self.store.count("agent_1", "user_1") == 0

    def test_tags_serialization(self):
        rec = _make_record(memory_id="m1", tags=["tag1", "tag2"])
        self.store.save(rec)
        fetched = self.store.get("m1")
        assert fetched.tags == ["tag1", "tag2"]

    def test_extra_serialization(self):
        rec = _make_record(memory_id="m1")
        rec.extra = {"key": "value"}
        self.store.save(rec)
        fetched = self.store.get("m1")
        assert fetched.extra == {"key": "value"}


# =====================================================================
# DefaultMemoryManager
# =====================================================================


class TestDefaultMemoryManager:
    def setup_method(self):
        self.store = SQLiteMemoryStore(db_path=":memory:")
        self.mgr = DefaultMemoryManager(store=self.store, max_memories_in_context=5)
        self.mgr.begin_session("run_1", "agent_1", "user_1")

    def teardown_method(self):
        self.store.close()

    # -- Pattern extraction --

    def test_extract_preference_pattern_english(self):
        candidates = self.mgr.extract_candidates("Always use Python for scripts", None, [])
        assert len(candidates) >= 1
        assert candidates[0].kind == MemoryKind.USER_PREFERENCE

    def test_extract_preference_pattern_chinese(self):
        candidates = self.mgr.extract_candidates("以后用Python回答", None, [])
        assert len(candidates) >= 1
        assert candidates[0].kind == MemoryKind.USER_PREFERENCE

    def test_extract_constraint_pattern(self):
        # "不要使用eval" matches constraint before preference patterns
        candidates = self.mgr.extract_candidates("不要使用eval", None, [])
        assert len(candidates) >= 1
        assert candidates[0].kind == MemoryKind.USER_CONSTRAINT

    def test_extract_constraint_pattern_chinese(self):
        candidates = self.mgr.extract_candidates("禁止使用eval", None, [])
        assert len(candidates) >= 1
        assert candidates[0].kind == MemoryKind.USER_CONSTRAINT

    def test_extract_project_pattern(self):
        candidates = self.mgr.extract_candidates("我们正在开发一个AI框架", None, [])
        assert len(candidates) >= 1
        assert candidates[0].kind == MemoryKind.PROJECT_CONTEXT

    def test_extract_no_match(self):
        candidates = self.mgr.extract_candidates("hello world", None, [])
        assert len(candidates) == 0

    def test_extract_empty_input(self):
        candidates = self.mgr.extract_candidates("", None, [])
        assert len(candidates) == 0

    # -- Merge rules --

    def test_merge_new_candidate_upserts(self):
        action = self.mgr.merge_candidate(
            _make_candidate(title="new"), []
        )
        assert action == MemoryUpdateAction.UPSERT

    def test_merge_same_content_ignores(self):
        existing = _make_record(kind=MemoryKind.USER_PREFERENCE, content="same content")
        action = self.mgr.merge_candidate(
            _make_candidate(content="same content"), [existing]
        )
        assert action == MemoryUpdateAction.IGNORE

    def test_merge_same_title_kind_different_content_upserts(self):
        existing = _make_record(
            kind=MemoryKind.USER_PREFERENCE, title="Test", content="old"
        )
        action = self.mgr.merge_candidate(
            _make_candidate(title="Test", content="new"), [existing]
        )
        assert action == MemoryUpdateAction.UPSERT

    def test_merge_pinned_record_ignores(self):
        existing = _make_record(
            kind=MemoryKind.USER_PREFERENCE, title="Test", content="old", is_pinned=True
        )
        action = self.mgr.merge_candidate(
            _make_candidate(title="Test", content="new"), [existing]
        )
        assert action == MemoryUpdateAction.IGNORE

    # -- select_for_context --

    def test_select_pinned_first(self):
        self.store.save(_make_record(memory_id="p", title="pinned", is_pinned=True))
        self.store.save(_make_record(memory_id="n", title="normal", is_pinned=False))
        state = _make_agent_state("pinned")
        result = self.mgr.select_for_context("pinned", state)
        assert len(result) >= 1
        assert result[0].is_pinned is True

    def test_select_limited_by_max(self):
        for i in range(10):
            self.store.save(_make_record(memory_id=f"m{i}", title=f"mem {i}"))
        state = _make_agent_state("mem")
        result = self.mgr.select_for_context("mem", state)
        assert len(result) <= 5

    def test_select_disabled_returns_empty(self):
        self.store.save(_make_record(memory_id="m1"))
        self.mgr.set_enabled(False)
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        assert result == []

    def test_select_keyword_matching(self):
        self.store.save(_make_record(memory_id="a", title="python tips", content="use type hints"))
        self.store.save(_make_record(memory_id="b", title="java tips", content="use generics"))
        state = _make_agent_state("python")
        result = self.mgr.select_for_context("python", state)
        assert len(result) >= 1
        # Python match should appear first (or at least be present)
        titles = [r.title for r in result]
        assert "python tips" in titles

    # -- record_turn --

    def test_record_turn_saves_preference(self):
        self.mgr.record_turn("Always use English", None, [])
        mems = self.store.list_by_user("agent_1", "user_1")
        assert len(mems) >= 1

    def test_record_turn_disabled_does_nothing(self):
        self.mgr.set_enabled(False)
        self.mgr.record_turn("Always use English", None, [])
        mems = self.store.list_by_user("agent_1", "user_1")
        assert len(mems) == 0

    # -- remember (base class) --

    def test_remember_creates_new_record(self):
        mid = self.mgr.remember(_make_candidate(title="new pref", content="val"))
        assert mid is not None
        mems = self.store.list_by_user("agent_1", "user_1", active_only=False)
        assert any(m.title == "new pref" for m in mems)

    def test_remember_updates_existing(self):
        # Create initial
        self.mgr.remember(_make_candidate(title="pref", content="v1"))
        # Update with same title + kind
        self.mgr.remember(_make_candidate(title="pref", content="v2"))
        mems = self.store.list_by_user("agent_1", "user_1", active_only=False)
        matching = [m for m in mems if m.title == "pref"]
        assert len(matching) == 1
        assert matching[0].content == "v2"
        assert matching[0].version == 2

    def test_remember_skips_pinned(self):
        # Create and pin
        mid = self.mgr.remember(_make_candidate(title="pref", content="v1"))
        self.mgr.pin(mid)
        # Attempt update
        self.mgr.remember(_make_candidate(title="pref", content="v2"))
        rec = self.store.get(mid)
        assert rec.content == "v1"  # unchanged

    def test_remember_disabled_returns_none(self):
        self.mgr.set_enabled(False)
        mid = self.mgr.remember(_make_candidate())
        assert mid is None

    # -- governance --

    def test_forget_deletes(self):
        mid = self.mgr.remember(_make_candidate(title="temp"))
        self.mgr.forget(mid)
        assert self.store.get(mid) is None

    def test_pin_and_unpin(self):
        mid = self.mgr.remember(_make_candidate(title="pin test"))
        self.mgr.pin(mid)
        assert self.store.get(mid).is_pinned is True
        self.mgr.unpin(mid)
        assert self.store.get(mid).is_pinned is False

    def test_activate_and_deactivate(self):
        mid = self.mgr.remember(_make_candidate(title="active test"))
        self.mgr.deactivate(mid)
        assert self.store.get(mid).is_active is False
        self.mgr.activate(mid)
        assert self.store.get(mid).is_active is True

    def test_clear_memories(self):
        self.mgr.remember(_make_candidate(title="a", content="content a"))
        self.mgr.remember(_make_candidate(title="b", content="content b"))
        count = self.mgr.clear_memories("agent_1", "user_1")
        assert count == 2
        assert self.store.count("agent_1", "user_1") == 0

    def test_list_memories(self):
        self.mgr.remember(_make_candidate(title="listed"))
        mems = self.mgr.list_memories("agent_1", "user_1")
        assert len(mems) >= 1

    def test_make_title_truncation(self):
        long_text = "a " * 50
        title = DefaultMemoryManager._make_title(long_text, max_len=20)
        assert len(title) <= 23  # 20 + "..."

    def test_end_session(self):
        self.mgr.end_session()
        assert self.mgr._run_id is None


# =====================================================================
# MemoryScope Managers
# =====================================================================


class TestIsolatedMemoryManager:
    def setup_method(self):
        self.store = SQLiteMemoryStore(db_path=":memory:")
        self.mgr = IsolatedMemoryManager(store=self.store)
        self.mgr.begin_session("run_1", "sub_1", None)

    def teardown_method(self):
        self.store.close()

    def test_select_returns_own_only(self):
        self.store.save(_make_record(memory_id="own", agent_id="sub_1", user_id=None))
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        assert len(result) == 1

    def test_record_turn_is_noop(self):
        self.mgr.record_turn("test", None, [])
        assert self.store.count("sub_1", None) == 0

    def test_extract_candidates_returns_empty(self):
        candidates = self.mgr.extract_candidates("test", None, [])
        assert candidates == []

    def test_merge_candidate_always_upserts(self):
        action = self.mgr.merge_candidate(_make_candidate(), [])
        assert action == MemoryUpdateAction.UPSERT

    def test_disabled_returns_empty(self):
        self.store.save(_make_record(memory_id="x", agent_id="sub_1", user_id=None))
        self.mgr.set_enabled(False)
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        assert result == []


class TestInheritReadMemoryManager:
    def setup_method(self):
        self.store = SQLiteMemoryStore(db_path=":memory:")
        self.parent_snapshot = [
            _make_record(memory_id="p1", title="parent_mem_1"),
            _make_record(memory_id="p2", title="parent_mem_2"),
            _make_record(memory_id="p3", title="parent_mem_3"),
        ]
        self.mgr = InheritReadMemoryManager(
            store=self.store,
            parent_snapshot=self.parent_snapshot,
            max_inherited=2,
        )
        self.mgr.begin_session("run_1", "sub_1", None)

    def teardown_method(self):
        self.store.close()

    def test_select_returns_inherited_plus_own(self):
        self.store.save(_make_record(memory_id="own", agent_id="sub_1", user_id=None, title="own_mem"))
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        # 2 inherited (max_inherited=2) + 1 own
        assert len(result) == 3
        # Inherited come first
        assert result[0].memory_id == "p1"
        assert result[1].memory_id == "p2"
        assert result[2].memory_id == "own"

    def test_max_inherited_limits_parent_snapshot(self):
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        # Only 2 of 3 parent records
        assert len(result) == 2

    def test_snapshot_is_frozen(self):
        """Modifying original list after construction doesn't affect manager."""
        self.parent_snapshot.append(
            _make_record(memory_id="p4", title="late_addition")
        )
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        ids = [r.memory_id for r in result]
        assert "p4" not in ids

    def test_record_turn_is_noop(self):
        self.mgr.record_turn("test", None, [])

    def test_extract_candidates_returns_empty(self):
        assert self.mgr.extract_candidates("test", None, []) == []


class TestSharedWriteMemoryManager:
    def setup_method(self):
        self.parent_mgr = MagicMock()
        self.snapshot = [
            _make_record(memory_id="s1", title="shared_1"),
            _make_record(memory_id="s2", title="shared_2"),
        ]
        self.mgr = SharedWriteMemoryManager(
            parent_manager=self.parent_mgr,
            parent_snapshot=self.snapshot,
        )
        self.mgr.begin_session("run_1", "sub_1", None)

    def test_select_returns_frozen_snapshot(self):
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        assert len(result) == 2
        assert result[0].memory_id == "s1"

    def test_snapshot_is_frozen_copy(self):
        """Modifying original snapshot list doesn't affect manager."""
        self.snapshot.append(_make_record(memory_id="s3"))
        state = _make_agent_state()
        result = self.mgr.select_for_context("test", state)
        assert len(result) == 2

    def test_remember_delegates_to_parent(self):
        candidate = _make_candidate()
        self.mgr.remember(candidate)
        # SharedWrite forces source_type="subagent" when delegating to parent
        self.parent_mgr.remember.assert_called_once()
        call_args = self.parent_mgr.remember.call_args
        assert call_args[0][0] == candidate
        source_ctx = call_args[1]["source_context"]
        assert source_ctx.source_type == "subagent"

    def test_forget_delegates_to_parent(self):
        self.mgr.forget("mid")
        self.parent_mgr.forget.assert_called_once_with("mid")

    def test_record_turn_does_not_delegate_to_parent(self):
        """SharedWrite record_turn returns local decision, not parent delegation.

        SubAgent extraction uses remember() with forced 'subagent' source,
        not parent's record_turn which uses parent's extraction patterns.
        """
        result = self.mgr.record_turn("input", "answer", [])
        self.parent_mgr.record_turn.assert_not_called()
        assert result.committed is False
        assert "SharedWrite" in result.reason

    def test_extract_candidates_returns_empty(self):
        assert self.mgr.extract_candidates("test", None, []) == []

    def test_end_session_is_noop(self):
        self.mgr.end_session()  # should not raise

    def test_begin_session_sets_fields(self):
        self.mgr.begin_session("r2", "a2", "u2")
        assert self.mgr._run_id == "r2"
        assert self.mgr._agent_id == "a2"
        assert self.mgr._user_id == "u2"


# =====================================================================
# MemoryPolicy application
# =====================================================================


class TestMemoryPolicyApplication:
    def setup_method(self):
        self.store = SQLiteMemoryStore(db_path=":memory:")
        self.mgr = DefaultMemoryManager(store=self.store)
        self.mgr.begin_session("run_1", "agent_1", "user_1")

    def teardown_method(self):
        self.store.close()

    def test_apply_policy_disables_memory(self):
        """memory_enabled=False should block remember()."""
        from agent_framework.models.agent import MemoryPolicy
        self.mgr.apply_memory_policy(MemoryPolicy(memory_enabled=False))
        c = MemoryCandidate(
            kind=MemoryKind.USER_PREFERENCE, title="pref", content="val",
        )
        assert self.mgr.remember(c) is None

    def test_apply_policy_disables_auto_extract(self):
        """auto_extract=False should skip extraction in record_turn."""
        from agent_framework.models.agent import MemoryPolicy
        self.mgr.apply_memory_policy(MemoryPolicy(auto_extract=False))
        result = self.mgr.record_turn("Always use Python", None, [])
        assert result.committed is False
        assert "disabled" in result.reason.lower()

    def test_apply_policy_max_in_context(self):
        """max_in_context limits number of memories returned by select_for_context."""
        from agent_framework.models.agent import AgentState, MemoryPolicy
        # Insert 5 memories
        for i in range(5):
            c = MemoryCandidate(
                kind=MemoryKind.CUSTOM, title=f"mem_{i}", content=f"content_{i}",
            )
            self.mgr.remember(c)
        # Policy: max 2
        self.mgr.apply_memory_policy(MemoryPolicy(max_in_context=2))
        state = AgentState(run_id="r1", task="test")
        selected = self.mgr.select_for_context("test", state)
        assert len(selected) <= 2


# =====================================================================
# MemoryQuota enforcement
# =====================================================================


class TestMemoryQuota:
    def setup_method(self):
        self.store = SQLiteMemoryStore(db_path=":memory:")
        self.mgr = DefaultMemoryManager(store=self.store)
        self.mgr.begin_session("run_1", "agent_1", "user_1")

    def teardown_method(self):
        self.store.close()

    def test_quota_content_length_rejects(self):
        """Content exceeding max_content_length should be rejected."""
        from agent_framework.models.agent import MemoryQuota
        self.mgr.set_quota(MemoryQuota(max_content_length=10))
        c = MemoryCandidate(
            kind=MemoryKind.CUSTOM, title="long", content="x" * 100,
        )
        assert self.mgr.remember(c) is None

    def test_quota_content_length_allows_short(self):
        """Content within max_content_length should be allowed."""
        from agent_framework.models.agent import MemoryQuota
        self.mgr.set_quota(MemoryQuota(max_content_length=100))
        c = MemoryCandidate(
            kind=MemoryKind.CUSTOM, title="short", content="hello",
        )
        assert self.mgr.remember(c) is not None

    def test_quota_max_items_rejects(self):
        """Exceeding max_items_per_user should reject new inserts."""
        from agent_framework.models.agent import MemoryQuota
        self.mgr.set_quota(MemoryQuota(max_items_per_user=2))
        for i in range(2):
            c = MemoryCandidate(
                kind=MemoryKind.CUSTOM, title=f"item_{i}", content=f"val_{i}",
            )
            assert self.mgr.remember(c) is not None
        # Third item should be rejected
        c = MemoryCandidate(
            kind=MemoryKind.CUSTOM, title="item_2", content="val_2",
        )
        assert self.mgr.remember(c) is None

    def test_quota_tags_truncated(self):
        """Tags exceeding max_tags_per_item should be truncated, not rejected."""
        from agent_framework.models.agent import MemoryQuota
        self.mgr.set_quota(MemoryQuota(max_tags_per_item=2))
        c = MemoryCandidate(
            kind=MemoryKind.CUSTOM, title="tagged", content="val",
            tags=["a", "b", "c", "d"],
        )
        mid = self.mgr.remember(c)
        assert mid is not None
        record = self.store.get(mid)
        assert len(record.tags) <= 2

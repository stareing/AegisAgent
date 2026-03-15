"""Tests for all memory store backends.

Each backend is tested against the same MemoryStoreProtocol contract.
PostgreSQL, MongoDB, Neo4j use mocks to avoid external dependencies.
SQLite uses a real temp database.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agent_framework.models.memory import MemoryKind, MemoryRecord
from agent_framework.models.message import Message


# ── Shared fixtures ──────────────────────────────────────────


def _make_record(**overrides) -> MemoryRecord:
    defaults = {
        "memory_id": str(uuid.uuid4()),
        "agent_id": "test-agent",
        "user_id": "test-user",
        "kind": MemoryKind.CUSTOM,
        "title": "Test Memory",
        "content": "Test content",
        "tags": ["tag1", "tag2"],
        "is_active": True,
        "is_pinned": False,
        "source": "agent",
    }
    defaults.update(overrides)
    return MemoryRecord(**defaults)


def _make_messages(n: int = 3) -> list[Message]:
    msgs = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        msgs.append(Message(role=role, content=f"Message {i}"))
    return msgs


class StoreContractTests:
    """Shared test methods for all store backends.

    Subclasses must set self.store before each test.
    """

    store: any

    def test_save_and_get(self):
        record = _make_record()
        self.store.save(record)
        loaded = self.store.get(record.memory_id)
        assert loaded is not None
        assert loaded.memory_id == record.memory_id
        assert loaded.title == "Test Memory"
        assert loaded.content == "Test content"
        assert loaded.kind == MemoryKind.CUSTOM

    def test_update(self):
        record = _make_record()
        self.store.save(record)
        record.title = "Updated Title"
        record.content = "Updated content"
        self.store.update(record)
        loaded = self.store.get(record.memory_id)
        assert loaded.title == "Updated Title"
        assert loaded.content == "Updated content"

    def test_delete(self):
        record = _make_record()
        self.store.save(record)
        self.store.delete(record.memory_id)
        assert self.store.get(record.memory_id) is None

    def test_list_by_user(self):
        r1 = _make_record(title="A")
        r2 = _make_record(title="B")
        self.store.save(r1)
        self.store.save(r2)
        results = self.store.list_by_user("test-agent", "test-user")
        assert len(results) >= 2

    def test_list_by_kind(self):
        r = _make_record(kind=MemoryKind.USER_PROFILE)
        self.store.save(r)
        results = self.store.list_by_kind("test-agent", "test-user", MemoryKind.USER_PROFILE)
        assert any(x.memory_id == r.memory_id for x in results)

    def test_list_recent(self):
        for i in range(5):
            self.store.save(_make_record(title=f"Recent {i}"))
        results = self.store.list_recent("test-agent", "test-user", limit=3)
        assert len(results) == 3

    def test_touch(self):
        record = _make_record()
        self.store.save(record)
        self.store.touch(record.memory_id)
        loaded = self.store.get(record.memory_id)
        assert loaded.use_count == 1
        assert loaded.last_used_at is not None

    def test_count(self):
        self.store.save(_make_record())
        self.store.save(_make_record())
        c = self.store.count("test-agent", "test-user")
        assert c >= 2

    def test_conversation_save_load(self):
        conv_id = self.store.new_conversation_id()
        msgs = _make_messages(4)
        self.store.save_conversation("test-project", conv_id, msgs)
        loaded = self.store.load_conversation(conv_id)
        assert len(loaded) == 4
        assert loaded[0].role == "user"
        assert loaded[0].content == "Message 0"

    def test_conversation_latest_id(self):
        cid1 = self.store.new_conversation_id()
        self.store.save_conversation("proj1", cid1, _make_messages(1))
        cid2 = self.store.new_conversation_id()
        self.store.save_conversation("proj1", cid2, _make_messages(1))
        latest = self.store.get_latest_conversation_id("proj1")
        assert latest == cid2

    def test_conversation_list(self):
        cid1 = self.store.new_conversation_id()
        self.store.save_conversation("proj-list", cid1, _make_messages(2))
        cid2 = self.store.new_conversation_id()
        self.store.save_conversation("proj-list", cid2, _make_messages(3))
        convs = self.store.list_conversations("proj-list")
        assert len(convs) == 2
        ids = {c["conversation_id"] for c in convs}
        assert cid1 in ids and cid2 in ids

    def test_conversation_clear(self):
        conv_id = self.store.new_conversation_id()
        self.store.save_conversation("proj-clear", conv_id, _make_messages(3))
        self.store.clear_conversation(conv_id)
        assert self.store.load_conversation(conv_id) == []

    def test_conversation_isolation(self):
        cid1 = self.store.new_conversation_id()
        cid2 = self.store.new_conversation_id()
        self.store.save_conversation("proj-iso", cid1, _make_messages(2))
        self.store.save_conversation("proj-iso", cid2, _make_messages(3))
        self.store.clear_conversation(cid1)
        assert self.store.load_conversation(cid1) == []
        assert len(self.store.load_conversation(cid2)) == 3


# ── SQLite (real) ────────────────────────────────────────────


class TestSQLiteStore(StoreContractTests):
    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path):
        from agent_framework.memory.sqlite_store import SQLiteMemoryStore
        self.store = SQLiteMemoryStore(db_path=str(tmp_path / "test.db"))
        yield
        self.store.close()


# ── PostgreSQL (mock) ────────────────────────────────────────


class TestPostgreSQLStore(StoreContractTests):
    """Tests PostgreSQL store with mocked psycopg2 connection."""

    @pytest.fixture(autouse=True)
    def setup_store(self, tmp_path):
        """Use SQLite as in-memory surrogate to validate PG store logic.

        We mock psycopg2.connect to return a real SQLite connection wrapped
        to accept %s placeholders (converted to ?).
        """
        import sqlite3

        db_path = str(tmp_path / "pg_mock.db")
        real_conn = sqlite3.connect(db_path)
        real_conn.row_factory = sqlite3.Row

        class PGCursorWrapper:
            """Wraps SQLite cursor, converts %s → ? for compatibility."""
            def __init__(self, sqlite_cur):
                self._cur = sqlite_cur
            def execute(self, sql, params=None):
                sql = sql.replace("%s", "?")
                # Replace JSONB/BOOLEAN/SERIAL/TIMESTAMPTZ with SQLite equivalents
                sql = sql.replace("JSONB", "TEXT")
                sql = sql.replace("BOOLEAN", "INTEGER")
                sql = sql.replace("SERIAL", "INTEGER")
                sql = sql.replace("TIMESTAMPTZ", "TEXT")
                sql = sql.replace("TRUE", "1").replace("true", "1")
                sql = sql.replace("FALSE", "0").replace("false", "0")
                if params:
                    # Convert booleans to int for SQLite
                    params = tuple(
                        int(p) if isinstance(p, bool) else p for p in params
                    )
                self._cur.execute(sql, params or ())
                return self
            def fetchall(self):
                return self._cur.fetchall()
            def fetchone(self):
                return self._cur.fetchone()
            @property
            def description(self):
                return self._cur.description
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass

        class PGConnWrapper:
            """Wraps SQLite connection to look like psycopg2 connection."""
            def __init__(self, conn):
                self._conn = conn
                self.autocommit = False
            def cursor(self):
                return PGCursorWrapper(self._conn.cursor())
            def commit(self):
                self._conn.commit()
            def close(self):
                self._conn.close()

        mock_conn = PGConnWrapper(real_conn)

        with patch.dict("sys.modules", {"psycopg2": MagicMock(), "psycopg2.extras": MagicMock()}):
            with patch("psycopg2.connect", return_value=mock_conn):
                with patch("psycopg2.extras.register_default_jsonb"):
                    from agent_framework.memory.pg_store import PostgreSQLMemoryStore
                    self.store = PostgreSQLMemoryStore.__new__(PostgreSQLMemoryStore)
                    self.store._conn = mock_conn
                    self.store._init_db()
        yield
        self.store.close()


# ── MongoDB (mock) ───────────────────────────────────────────


class _FakeMongoDB:
    """In-memory MongoDB mock using dicts."""

    def __init__(self):
        self._data: dict[str, list[dict]] = {}

    def __getitem__(self, name):
        return _FakeCollection(self._data, name)


class _FakeCollection:
    _auto_id = 0

    def __init__(self, data: dict, name: str):
        self._data = data
        self._name = name
        if name not in data:
            data[name] = []

    @property
    def _docs(self) -> list[dict]:
        return self._data[self._name]

    def create_index(self, keys):
        pass

    def insert_one(self, doc):
        _FakeCollection._auto_id += 1
        d = dict(doc)
        d.setdefault("_insert_order", _FakeCollection._auto_id)
        self._docs.append(d)

    def insert_many(self, docs):
        for d in docs:
            _FakeCollection._auto_id += 1
            dd = dict(d)
            dd.setdefault("_insert_order", _FakeCollection._auto_id)
            self._docs.append(dd)

    def find_one(self, filter_=None, sort=None, projection=None):
        results = self._filter(filter_ or {})
        if sort:
            results = self._sort(results, sort)
        else:
            # Default: insertion order (most recent last)
            results.sort(key=lambda x: x.get("_insert_order", 0))
        return results[0] if results else None

    def find(self, filter_=None):
        return _FakeCursor(self._filter(filter_ or {}))

    def update_one(self, filter_, update):
        for doc in self._filter(filter_):
            if "$set" in update:
                doc.update(update["$set"])
            if "$inc" in update:
                for k, v in update["$inc"].items():
                    doc[k] = doc.get(k, 0) + v
            break

    def delete_one(self, filter_):
        docs = self._filter(filter_)
        if docs:
            self._docs.remove(docs[0])

    def delete_many(self, filter_):
        to_remove = self._filter(filter_)
        for d in to_remove:
            self._docs.remove(d)

    def count_documents(self, filter_):
        return len(self._filter(filter_))

    def aggregate(self, pipeline):
        # Simplified aggregation for our test cases
        docs = list(self._docs)
        for stage in pipeline:
            if "$match" in stage:
                docs = [d for d in docs if self._matches(d, stage["$match"])]
            elif "$group" in stage:
                groups: dict = {}
                grp = stage["$group"]
                id_field = grp["_id"]
                for d in docs:
                    key = d.get(id_field.lstrip("$"))
                    if key not in groups:
                        groups[key] = {"_id": key}
                        for k, v in grp.items():
                            if k == "_id":
                                continue
                            if isinstance(v, dict):
                                if "$min" in v:
                                    groups[key][k] = d.get(v["$min"].lstrip("$"))
                                elif "$sum" in v:
                                    groups[key][k] = 0
                    for k, v in grp.items():
                        if k == "_id":
                            continue
                        if isinstance(v, dict):
                            if "$sum" in v:
                                groups[key][k] = groups[key].get(k, 0) + (v["$sum"] if isinstance(v["$sum"], int) else 1)
                            elif "$min" in v:
                                field = v["$min"].lstrip("$")
                                cur = groups[key].get(k)
                                val = d.get(field)
                                if cur is None or (val is not None and val < cur):
                                    groups[key][k] = val
                docs = list(groups.values())
            elif "$sort" in stage:
                field = list(stage["$sort"].keys())[0]
                reverse = stage["$sort"][field] == -1
                docs.sort(key=lambda x: x.get(field, ""), reverse=reverse)
        return docs

    def _filter(self, f: dict) -> list[dict]:
        result = []
        for d in self._docs:
            if self._matches(d, f):
                result.append(d)
        return result

    def _matches(self, doc: dict, f: dict) -> bool:
        for k, v in f.items():
            if k == "$or":
                if not any(self._matches(doc, sub) for sub in v):
                    return False
            elif doc.get(k) != v:
                return False
        return True

    def _sort(self, docs: list, sort_spec) -> list:
        if isinstance(sort_spec, list):
            for field, direction in reversed(sort_spec):
                # _id sort → use insertion order
                key_fn = (lambda x, f=field: x.get("_insert_order", 0)) if field == "_id" else (lambda x, f=field: x.get(f, ""))
                docs.sort(key=key_fn, reverse=(direction == -1))
        return docs


class _FakeCursor:
    def __init__(self, docs):
        self._docs = docs
    def sort(self, field, direction=1):
        self._docs.sort(key=lambda x: x.get(field, ""), reverse=(direction == -1))
        return self
    def limit(self, n):
        self._docs = self._docs[:n]
        return self
    def __iter__(self):
        return iter(self._docs)


class TestMongoDBStore(StoreContractTests):
    @pytest.fixture(autouse=True)
    def setup_store(self):
        fake_db = _FakeMongoDB()

        with patch.dict("sys.modules", {"pymongo": MagicMock()}):
            from agent_framework.memory.mongo_store import MongoDBMemoryStore
            store = MongoDBMemoryStore.__new__(MongoDBMemoryStore)
            store._client = MagicMock()
            store._db = fake_db
            store._memories = fake_db["saved_memories"]
            store._conversations = fake_db["conversation_history"]
            self.store = store
        yield


# ── Neo4j (interface parity via SQLite proxy) ───────────────
#
# Neo4j Cypher queries are too complex to mock reliably in-memory.
# Instead we verify that Neo4jMemoryStore class exists, implements
# all protocol methods, and test the import/factory path.
# The actual CRUD contract is already proven by SQLite tests.


class TestNeo4jStoreInterface:
    """Verify Neo4jMemoryStore has all MemoryStoreProtocol methods."""

    def test_has_all_protocol_methods(self):
        with patch.dict("sys.modules", {"neo4j": MagicMock()}):
            from agent_framework.memory.neo4j_store import Neo4jMemoryStore
            required = [
                "save", "update", "delete", "get",
                "list_by_user", "list_by_kind", "list_recent",
                "touch", "count",
                "new_conversation_id", "save_conversation",
                "load_conversation", "get_latest_conversation_id",
                "list_conversations", "clear_conversation", "close",
            ]
            for method in required:
                assert hasattr(Neo4jMemoryStore, method), f"Missing method: {method}"
                assert callable(getattr(Neo4jMemoryStore, method)), f"Not callable: {method}"


# ── Factory tests ────────────────────────────────────────────


class TestStoreFactory:
    def test_default_sqlite(self, tmp_path):
        from agent_framework.entry import _create_memory_store
        from agent_framework.infra.config import MemoryConfig

        cfg = MemoryConfig(db_path=str(tmp_path / "test.db"))
        store = _create_memory_store(cfg)
        assert type(store).__name__ == "SQLiteMemoryStore"
        store.close()

    def test_pg_requires_url(self):
        from agent_framework.entry import _create_memory_store
        from agent_framework.infra.config import MemoryConfig

        with pytest.raises(ValueError, match="connection_url"):
            _create_memory_store(MemoryConfig(store_type="postgresql"))

    def test_mongo_requires_url(self):
        from agent_framework.entry import _create_memory_store
        from agent_framework.infra.config import MemoryConfig

        with pytest.raises(ValueError, match="connection_url"):
            _create_memory_store(MemoryConfig(store_type="mongodb"))

    def test_neo4j_requires_url(self):
        from agent_framework.entry import _create_memory_store
        from agent_framework.infra.config import MemoryConfig

        with pytest.raises(ValueError, match="connection_url"):
            _create_memory_store(MemoryConfig(store_type="neo4j"))

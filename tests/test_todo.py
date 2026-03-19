"""Comprehensive tests for persistent task graph (TaskManager + TaskService).

Covers:
1. Task CRUD and dependency graph
2. Dependency auto-unblock on completion
3. Run-id isolation
4. Reminder after 3 rounds without task tool call
5. Reminder enters system_core, not user messages
6. Non-task flows are not polluted
7. Disk persistence
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from agent_framework.tools.todo import TaskManager, TaskService, TaskStatus


@pytest.fixture
def tmp_tasks_dir(tmp_path):
    """Provide a temporary directory for task files."""
    return tmp_path / ".tasks"


@pytest.fixture
def mgr(tmp_tasks_dir):
    """Fresh TaskManager backed by a temp directory."""
    return TaskManager(tmp_tasks_dir)


# ══════════════════════════════════════════════════════════════════
# 1. Task CRUD
# ══════════════════════════════════════════════════════════════════


class TestTaskCRUD:
    def test_create_basic(self, mgr):
        result = json.loads(mgr.create("Setup project"))
        assert result["id"] == 1
        assert result["subject"] == "Setup project"
        assert result["status"] == "pending"
        assert result["blockedBy"] == []
        assert result["blocks"] == []

    def test_create_with_description(self, mgr):
        result = json.loads(mgr.create("Build API", "REST endpoints for users"))
        assert result["description"] == "REST endpoints for users"

    def test_create_increments_id(self, mgr):
        t1 = json.loads(mgr.create("First"))
        t2 = json.loads(mgr.create("Second"))
        assert t1["id"] == 1
        assert t2["id"] == 2

    def test_get_existing(self, mgr):
        mgr.create("Task A")
        result = json.loads(mgr.get(1))
        assert result["subject"] == "Task A"

    def test_get_not_found(self, mgr):
        result = json.loads(mgr.get(999))
        assert "error" in result

    def test_update_status(self, mgr):
        mgr.create("Task A")
        result = json.loads(mgr.update(1, status="in_progress"))
        assert result["status"] == "in_progress"

    def test_update_subject(self, mgr):
        mgr.create("Old name")
        result = json.loads(mgr.update(1, subject="New name"))
        assert result["subject"] == "New name"

    def test_update_not_found(self, mgr):
        result = json.loads(mgr.update(999, status="completed"))
        assert "error" in result

    def test_list_all_empty(self, mgr):
        result = json.loads(mgr.list_all())
        assert result["summary"]["total"] == 0
        assert result["tasks"] == []

    def test_list_all_with_tasks(self, mgr):
        mgr.create("A")
        mgr.create("B")
        mgr.create("C")
        result = json.loads(mgr.list_all())
        assert result["summary"]["total"] == 3
        assert result["summary"]["ready"] == 3

    def test_update_owner(self, mgr):
        mgr.create("Task A")
        result = json.loads(mgr.update(1, owner="agent-1"))
        assert result["owner"] == "agent-1"

    # ── New fields aligned with Claude Code TaskCreate/TaskUpdate ──

    def test_create_with_active_form(self, mgr):
        result = json.loads(mgr.create("Run tests", active_form="Running tests"))
        assert result["activeForm"] == "Running tests"

    def test_create_with_metadata(self, mgr):
        result = json.loads(mgr.create("Deploy", metadata={"env": "prod", "priority": "p0"}))
        assert result["metadata"]["env"] == "prod"
        assert result["metadata"]["priority"] == "p0"

    def test_update_active_form(self, mgr):
        mgr.create("Task A")
        result = json.loads(mgr.update(1, active_form="Working on A"))
        assert result["activeForm"] == "Working on A"

    def test_update_metadata_merge(self, mgr):
        mgr.create("Task A", metadata={"key1": "v1", "key2": "v2"})
        result = json.loads(mgr.update(1, metadata={"key2": "updated", "key3": "new"}))
        assert result["metadata"] == {"key1": "v1", "key2": "updated", "key3": "new"}

    def test_update_metadata_delete_key(self, mgr):
        mgr.create("Task A", metadata={"keep": "yes", "drop": "me"})
        result = json.loads(mgr.update(1, metadata={"drop": None}))
        assert "drop" not in result["metadata"]
        assert result["metadata"]["keep"] == "yes"

    def test_delete_status(self, mgr):
        mgr.create("Task A")
        result = json.loads(mgr.update(1, status="deleted"))
        assert result["status"] == "deleted"
        # File should be gone
        assert json.loads(mgr.get(1)).get("error") is not None

    def test_delete_cleans_dependency_edges(self, mgr):
        mgr.create("A")
        mgr.create("B", blocked_by=[1])
        mgr.create("C", blocked_by=[2])
        # Delete B — should remove B from C's blockedBy and A's blocks
        mgr.update(2, status="deleted")
        c = json.loads(mgr.get(3))
        assert 2 not in c["blockedBy"]
        a = json.loads(mgr.get(1))
        assert 2 not in a["blocks"]

    def test_delete_not_in_list(self, mgr):
        mgr.create("A")
        mgr.create("B")
        mgr.update(1, status="deleted")
        result = json.loads(mgr.list_all())
        assert result["summary"]["total"] == 1
        ids = [t["id"] for t in result["tasks"]]
        assert 1 not in ids

    # ── PRD §6.1.5 Validation rules ──────────────────────────────

    def test_empty_subject_raises(self, mgr):
        with pytest.raises(ValueError, match="subject is required"):
            mgr.create("")

    def test_whitespace_subject_raises(self, mgr):
        with pytest.raises(ValueError, match="subject is required"):
            mgr.create("   ")

    def test_max_items_enforced(self, tmp_tasks_dir):
        mgr = TaskManager(tmp_tasks_dir, max_items=3)
        mgr.create("A")
        mgr.create("B")
        mgr.create("C")
        with pytest.raises(ValueError, match="Maximum 3 tasks"):
            mgr.create("D")

    def test_single_in_progress_enforced(self, mgr):
        mgr.create("A")
        mgr.create("B")
        mgr.update(1, status="in_progress")
        with pytest.raises(ValueError, match="Only one task can be in_progress"):
            mgr.update(2, status="in_progress")

    def test_single_in_progress_allows_same_task(self, mgr):
        """Updating the same in_progress task is fine."""
        mgr.create("A")
        mgr.update(1, status="in_progress")
        result = json.loads(mgr.update(1, subject="A updated"))
        assert result["status"] == "in_progress"

    def test_single_in_progress_after_completing_first(self, mgr):
        """Can start new in_progress after completing the current one."""
        mgr.create("A")
        mgr.create("B")
        mgr.update(1, status="in_progress")
        mgr.update(1, status="completed")
        result = json.loads(mgr.update(2, status="in_progress"))
        assert result["status"] == "in_progress"

    def test_invalid_status_raises(self, mgr):
        mgr.create("A")
        with pytest.raises(ValueError, match="Invalid status"):
            mgr.update(1, status="bogus")


# ══════════════════════════════════════════════════════════════════
# 2. Dependency graph
# ══════════════════════════════════════════════════════════════════


class TestDependencyGraph:
    def test_create_with_dependency(self, mgr):
        mgr.create("Task A")
        t2 = json.loads(mgr.create("Task B", blocked_by=[1]))
        assert t2["blockedBy"] == [1]

        # Forward edge registered on task A
        t1 = json.loads(mgr.get(1))
        assert 2 in t1["blocks"]

    def test_linear_chain(self, mgr):
        """A → B → C"""
        mgr.create("A")
        mgr.create("B", blocked_by=[1])
        mgr.create("C", blocked_by=[2])

        result = json.loads(mgr.list_all())
        assert result["ready_task_ids"] == [1]
        assert set(result["blocked_task_ids"]) == {2, 3}

    def test_fan_out(self, mgr):
        """A → B, A → C (parallel after A)"""
        mgr.create("A")
        mgr.create("B", blocked_by=[1])
        mgr.create("C", blocked_by=[1])

        result = json.loads(mgr.list_all())
        assert result["ready_task_ids"] == [1]
        assert set(result["blocked_task_ids"]) == {2, 3}

    def test_fan_in(self, mgr):
        """B, C → D (D waits for both)"""
        mgr.create("A")
        mgr.create("B", blocked_by=[1])
        mgr.create("C", blocked_by=[1])
        mgr.create("D", blocked_by=[2, 3])

        result = json.loads(mgr.list_all())
        assert result["ready_task_ids"] == [1]
        assert set(result["blocked_task_ids"]) == {2, 3, 4}

    def test_complete_unblocks_dependents(self, mgr):
        """Completing A unblocks B and C."""
        mgr.create("A")
        mgr.create("B", blocked_by=[1])
        mgr.create("C", blocked_by=[1])

        mgr.update(1, status="completed")

        result = json.loads(mgr.list_all())
        assert result["summary"]["completed"] == 1
        # B and C are now ready (no more blockedBy)
        assert set(result["ready_task_ids"]) == {2, 3}

    def test_partial_unblock(self, mgr):
        """D blocked by B and C. Completing B only partially unblocks D."""
        mgr.create("A")
        mgr.create("B", blocked_by=[1])
        mgr.create("C", blocked_by=[1])
        mgr.create("D", blocked_by=[2, 3])

        mgr.update(1, status="completed")  # unblocks B, C
        mgr.update(2, status="completed")  # unblocks D partially

        d = json.loads(mgr.get(4))
        assert d["blockedBy"] == [3]  # still blocked by C

        mgr.update(3, status="completed")  # fully unblocks D
        d = json.loads(mgr.get(4))
        assert d["blockedBy"] == []

    def test_add_dependency_via_update(self, mgr):
        mgr.create("A")
        mgr.create("B")
        mgr.update(2, add_blocked_by=[1])

        b = json.loads(mgr.get(2))
        assert 1 in b["blockedBy"]

        a = json.loads(mgr.get(1))
        assert 2 in a["blocks"]

    def test_add_blocks_via_update(self, mgr):
        mgr.create("A")
        mgr.create("B")
        mgr.update(1, add_blocks=[2])

        a = json.loads(mgr.get(1))
        assert 2 in a["blocks"]

        b = json.loads(mgr.get(2))
        assert 1 in b["blockedBy"]


# ══════════════════════════════════════════════════════════════════
# 3. Run isolation via TaskService
# ══════════════════════════════════════════════════════════════════


class TestRunIsolation:
    def test_shared_project_tasks(self, tmp_tasks_dir):
        """All runs share the same project .tasks/ directory."""
        service = TaskService(tmp_tasks_dir)
        mgr1 = service.get("run-1")
        mgr1.create("Shared task")

        mgr2 = service.get("run-2")
        result = json.loads(mgr2.list_all())
        assert result["summary"]["total"] == 1

    def test_remove_releases_reference(self, tmp_tasks_dir):
        service = TaskService(tmp_tasks_dir)
        service.get("run-1").create("Task")
        service.remove("run-1")

        # Files persist — new manager reads them
        mgr = service.get("run-1")
        result = json.loads(mgr.list_all())
        assert result["summary"]["total"] == 1


# ══════════════════════════════════════════════════════════════════
# 4. Reminder logic
# ══════════════════════════════════════════════════════════════════


class TestReminder:
    def test_no_reminder_initially(self, mgr):
        mgr.create("Task")
        assert not mgr.should_remind()

    def test_no_reminder_after_2_rounds(self, mgr):
        mgr.create("Task")
        mgr.mark_round(wrote_task=False)
        mgr.mark_round(wrote_task=False)
        assert not mgr.should_remind()

    def test_reminder_after_3_rounds(self, mgr):
        mgr.create("Task")
        mgr.mark_round(wrote_task=False)
        mgr.mark_round(wrote_task=False)
        mgr.mark_round(wrote_task=False)
        assert mgr.should_remind()

    def test_reminder_resets_on_create(self, mgr):
        mgr.create("A")
        mgr.mark_round(wrote_task=False)
        mgr.mark_round(wrote_task=False)
        mgr.mark_round(wrote_task=False)
        assert mgr.should_remind()

        mgr.create("B")  # resets counter
        assert not mgr.should_remind()

    def test_no_reminder_without_tasks(self, mgr):
        mgr.mark_round(wrote_task=False)
        mgr.mark_round(wrote_task=False)
        mgr.mark_round(wrote_task=False)
        assert not mgr.should_remind()


# ══════════════════════════════════════════════════════════════════
# 5. Context injection
# ══════════════════════════════════════════════════════════════════


class TestContextInjection:
    def test_summary_text(self, mgr):
        mgr.create("A")
        mgr.create("B", blocked_by=[1])
        mgr.update(1, status="completed")

        text = mgr.summary_text()
        assert "1/2 done" in text
        assert "1 ready" in text

    def test_summary_text_empty(self, mgr):
        assert mgr.summary_text() == ""

    def test_todo_state_in_system_core(self):
        from agent_framework.context.source_provider import \
            ContextSourceProvider
        from agent_framework.models.agent import AgentConfig

        provider = ContextSourceProvider()
        config = AgentConfig()
        runtime_info = {
            "todo_summary": "2/4 done, 1 active, 1 ready",
            "todo_reminder": "Update your tasks.",
        }
        result = provider.collect_system_core(config, runtime_info)
        assert "<todo-state>" in result
        assert "<summary>" in result
        assert "<reminder>" in result

    def test_no_todo_state_without_info(self):
        from agent_framework.context.source_provider import \
            ContextSourceProvider
        from agent_framework.models.agent import AgentConfig

        provider = ContextSourceProvider()
        result = provider.collect_system_core(AgentConfig(), {"operating_system": "Linux"})
        assert "<todo-state>" not in result


# ══════════════════════════════════════════════════════════════════
# 6. Non-task flows not polluted
# ══════════════════════════════════════════════════════════════════


class TestNonTaskFlowClean:
    def test_empty_manager_no_summary(self, mgr):
        assert mgr.summary_text() == ""
        assert not mgr.has_tasks

    def test_service_get_creates_clean_manager(self, tmp_tasks_dir):
        service = TaskService(tmp_tasks_dir)
        mgr = service.get("fresh-run")
        assert not mgr.has_tasks
        assert not mgr.should_remind()


# ══════════════════════════════════════════════════════════════════
# 7. Disk persistence
# ══════════════════════════════════════════════════════════════════


class TestDiskPersistence:
    def test_tasks_survive_manager_recreation(self, tmp_tasks_dir):
        mgr1 = TaskManager(tmp_tasks_dir)
        mgr1.create("Persistent task")
        mgr1.create("Another", blocked_by=[1])

        # New manager reads the same directory
        mgr2 = TaskManager(tmp_tasks_dir)
        result = json.loads(mgr2.list_all())
        assert result["summary"]["total"] == 2

    def test_id_counter_survives_restart(self, tmp_tasks_dir):
        mgr1 = TaskManager(tmp_tasks_dir)
        mgr1.create("First")
        mgr1.create("Second")

        mgr2 = TaskManager(tmp_tasks_dir)
        t3 = json.loads(mgr2.create("Third"))
        assert t3["id"] == 3

    def test_completion_persists(self, tmp_tasks_dir):
        mgr1 = TaskManager(tmp_tasks_dir)
        mgr1.create("Task")
        mgr1.update(1, status="completed")

        mgr2 = TaskManager(tmp_tasks_dir)
        t = json.loads(mgr2.get(1))
        assert t["status"] == "completed"

    def test_dependency_unblock_persists(self, tmp_tasks_dir):
        mgr1 = TaskManager(tmp_tasks_dir)
        mgr1.create("A")
        mgr1.create("B", blocked_by=[1])
        mgr1.update(1, status="completed")

        mgr2 = TaskManager(tmp_tasks_dir)
        b = json.loads(mgr2.get(2))
        assert b["blockedBy"] == []

    def test_single_json_file(self, tmp_tasks_dir):
        mgr = TaskManager(tmp_tasks_dir)
        mgr.create("Task 1")
        mgr.create("Task 2")

        # Single file, not per-task files
        assert (tmp_tasks_dir / "tasks.json").exists()
        assert not list(tmp_tasks_dir.glob("task_*.json"))

        # File contains both tasks
        import json as _json
        data = _json.loads((tmp_tasks_dir / "tasks.json").read_text())
        assert len(data["tasks"]) == 2
        assert data["next_id"] == 3


# ══════════════════════════════════════════════════════════════════
# 8. ToolExecutor routing
# ══════════════════════════════════════════════════════════════════


class TestToolExecutorRouting:
    def test_executor_has_task_service(self):
        from unittest.mock import MagicMock

        from agent_framework.tools.executor import ToolExecutor

        registry = MagicMock()
        executor = ToolExecutor(registry)
        assert hasattr(executor, "_todo_service")

    def test_task_tool_names(self):
        from agent_framework.tools.executor import ToolExecutor
        assert "task_create" in ToolExecutor._TASK_TOOLS
        assert "task_update" in ToolExecutor._TASK_TOOLS
        assert "task_list" in ToolExecutor._TASK_TOOLS
        assert "task_get" in ToolExecutor._TASK_TOOLS


# ══════════════════════════════════════════════════════════════════
# 9. Model-driven planning — tools always visible
# ══════════════════════════════════════════════════════════════════


class TestModelDrivenPlanning:
    def test_task_create_description_guides_proactive_use(self):
        from agent_framework.tools.builtin.task_manager import task_create
        meta = task_create.__tool_meta__
        desc = meta.description.lower()
        assert "proactively" in desc or "use proactively" in desc.replace("use ", "use ")
        assert "3+" in desc or "3 or more" in desc

    def test_task_tools_always_registered(self):
        from unittest.mock import MagicMock

        from agent_framework.tools.builtin import register_all_builtins

        catalog = MagicMock()
        register_all_builtins(catalog)
        registered_names = []
        for call in catalog.register_function.call_args_list:
            fn = call.args[0] if call.args else None
            if fn and hasattr(fn, "__tool_meta__"):
                registered_names.append(fn.__tool_meta__.name)
        assert "task_create" in registered_names
        assert "task_update" in registered_names
        assert "task_list" in registered_names
        assert "task_get" in registered_names


# ══════════════════════════════════════════════════════════════════
# 10. TodoConfig integration
# ══════════════════════════════════════════════════════════════════


class TestTodoConfig:
    def test_config_exists_in_framework(self):
        from agent_framework.infra.config import FrameworkConfig, TodoConfig
        cfg = FrameworkConfig()
        assert hasattr(cfg, "todo")
        assert isinstance(cfg.todo, TodoConfig)

    def test_config_defaults(self):
        from agent_framework.infra.config import TodoConfig
        cfg = TodoConfig()
        assert cfg.enabled is True
        assert cfg.max_items == 20
        assert cfg.reminder_threshold_rounds == 3
        assert cfg.inject_reminder is True

    def test_config_flows_to_task_manager(self, tmp_tasks_dir):
        mgr = TaskManager(tmp_tasks_dir, max_items=5, reminder_threshold=2)
        assert mgr._max_items == 5
        assert mgr._reminder_threshold == 2

    def test_config_flows_to_service(self, tmp_tasks_dir):
        service = TaskService(tmp_tasks_dir, max_items=5, reminder_threshold=2)
        mgr = service.get("run-1")
        assert mgr._max_items == 5
        assert mgr._reminder_threshold == 2

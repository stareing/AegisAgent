"""Tests for three-mode result collection strategies (Mode A/B/C).

Covers:
- LeadCollector SEQUENTIAL/BATCH_ALL/HYBRID modes
- BatchResult structure and counters
- Degradation: HYBRID → SEQUENTIAL (1 complete), HYBRID → BATCH_ALL (all at once)
- ToolExecutor integration: spawn_agent with collection_strategy, check_spawn_result with batch_pull
- SpawnAgentArgs and CheckSpawnResultArgs schema
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent_framework.subagent.lead_collector import (
    BatchResult,
    CollectionStrategy,
    LeadCollector,
)


# ---------------------------------------------------------------------------
# LeadCollector Unit Tests
# ---------------------------------------------------------------------------

class TestLeadCollectorSequential:
    """Mode A: collect one at a time."""

    @pytest.mark.asyncio
    async def test_sequential_returns_one_result(self):
        collector = LeadCollector(strategy=CollectionStrategy.SEQUENTIAL)
        collector.register_spawn("sp1", "Task 1", "Agent A")
        collector.register_spawn("sp2", "Task 2", "Agent B")

        call_count = 0

        async def collect_fn(spawn_id: str, wait: bool = False):
            nonlocal call_count
            call_count += 1
            if spawn_id == "sp1":
                return {"status": "COMPLETED", "summary": "Done 1"}
            return None  # sp2 still running

        batch = await collector.pull(collect_fn)
        assert len(batch.results) == 1
        assert batch.results[0]["summary"] == "Done 1"
        assert batch.total_collected == 1
        assert batch.still_running == 1
        assert not batch.is_final_batch

    @pytest.mark.asyncio
    async def test_sequential_second_pull_gets_next(self):
        collector = LeadCollector(strategy=CollectionStrategy.SEQUENTIAL)
        collector.register_spawn("sp1", "Task 1", "Agent A")
        collector.register_spawn("sp2", "Task 2", "Agent B")

        results_available = {"sp1": True, "sp2": False}

        async def collect_fn(spawn_id: str, wait: bool = False):
            if results_available.get(spawn_id):
                return {"status": "COMPLETED", "summary": f"Done {spawn_id}"}
            return None

        # First pull: sp1
        batch1 = await collector.pull(collect_fn)
        assert len(batch1.results) == 1
        assert batch1.batch_index == 1

        # Make sp2 available
        results_available["sp2"] = True

        # Second pull: sp2
        batch2 = await collector.pull(collect_fn)
        assert len(batch2.results) == 1
        assert batch2.is_final_batch
        assert batch2.batch_index == 2


class TestLeadCollectorBatchAll:
    """Mode B: wait for all, return all at once."""

    @pytest.mark.asyncio
    async def test_batch_all_waits_and_returns_all(self):
        collector = LeadCollector(strategy=CollectionStrategy.BATCH_ALL)
        collector.register_spawn("sp1", "Task 1", "Agent A")
        collector.register_spawn("sp2", "Task 2", "Agent B")
        collector.register_spawn("sp3", "Task 3", "Agent C")

        async def collect_fn(spawn_id: str, wait: bool = False):
            # BATCH_ALL calls with wait=True
            return {"status": "COMPLETED", "summary": f"Done {spawn_id}"}

        batch = await collector.pull(collect_fn)
        assert len(batch.results) == 3
        assert batch.total_collected == 3
        assert batch.still_running == 0
        assert batch.is_final_batch


class TestLeadCollectorHybrid:
    """Mode C: collect all currently-completed."""

    @pytest.mark.asyncio
    async def test_hybrid_returns_all_completed(self):
        collector = LeadCollector(strategy=CollectionStrategy.HYBRID, poll_interval_ms=10)
        collector.register_spawn("sp1", "Task 1", "Agent A")
        collector.register_spawn("sp2", "Task 2", "Agent B")
        collector.register_spawn("sp3", "Task 3", "Agent C")

        # sp1 and sp2 are done, sp3 still running
        async def collect_fn(spawn_id: str, wait: bool = False):
            if spawn_id in ("sp1", "sp2"):
                return {"status": "COMPLETED", "summary": f"Done {spawn_id}"}
            return None

        batch = await collector.pull(collect_fn)
        assert len(batch.results) == 2
        assert batch.total_collected == 2
        assert batch.still_running == 1
        assert not batch.is_final_batch

    @pytest.mark.asyncio
    async def test_hybrid_degrades_to_sequential(self):
        """When only 1 completes, hybrid returns just that one (Mode A behavior)."""
        collector = LeadCollector(strategy=CollectionStrategy.HYBRID, poll_interval_ms=10)
        collector.register_spawn("sp1", "Task 1")
        collector.register_spawn("sp2", "Task 2")

        async def collect_fn(spawn_id: str, wait: bool = False):
            if spawn_id == "sp1":
                return {"status": "COMPLETED", "summary": "Done sp1"}
            return None

        batch = await collector.pull(collect_fn)
        assert len(batch.results) == 1  # Degrades to Mode A

    @pytest.mark.asyncio
    async def test_hybrid_degrades_to_batch_all(self):
        """When all complete simultaneously, hybrid returns all (Mode B behavior)."""
        collector = LeadCollector(strategy=CollectionStrategy.HYBRID, poll_interval_ms=10)
        collector.register_spawn("sp1", "Task 1")
        collector.register_spawn("sp2", "Task 2")

        async def collect_fn(spawn_id: str, wait: bool = False):
            return {"status": "COMPLETED", "summary": f"Done {spawn_id}"}

        batch = await collector.pull(collect_fn)
        assert len(batch.results) == 2  # Degrades to Mode B
        assert batch.is_final_batch


class TestLeadCollectorEdgeCases:

    @pytest.mark.asyncio
    async def test_empty_pull_no_spawns(self):
        collector = LeadCollector(strategy=CollectionStrategy.HYBRID)
        batch = await collector.pull(AsyncMock())
        assert len(batch.results) == 0
        assert batch.total_spawned == 0

    def test_progress_summary(self):
        collector = LeadCollector()
        collector.register_spawn("sp1", "Task 1", "A")
        collector.register_spawn("sp2", "Task 2", "B")
        assert collector.get_progress_summary() == "0/2 completed, 2 running"

    def test_reset(self):
        collector = LeadCollector()
        collector.register_spawn("sp1", "Task 1")
        assert collector.total_spawned == 1
        collector.reset()
        assert collector.total_spawned == 0

    @pytest.mark.asyncio
    async def test_batch_result_labels_preserved(self):
        collector = LeadCollector(strategy=CollectionStrategy.SEQUENTIAL)
        collector.register_spawn("sp1", "Task 1", "Agent A — shell.py")

        async def collect_fn(spawn_id: str, wait: bool = False):
            return {"status": "COMPLETED", "summary": "Done"}

        batch = await collector.pull(collect_fn)
        assert batch.results[0]["_spawn_label"] == "Agent A — shell.py"
        assert batch.results[0]["_spawn_id"] == "sp1"


# ---------------------------------------------------------------------------
# CollectionStrategy Enum
# ---------------------------------------------------------------------------

class TestCollectionStrategyEnum:
    """CollectionStrategy enum in subagent models."""

    def test_all_values(self):
        from agent_framework.models.subagent import CollectionStrategy as CS
        assert CS.SEQUENTIAL.value == "SEQUENTIAL"
        assert CS.BATCH_ALL.value == "BATCH_ALL"
        assert CS.HYBRID.value == "HYBRID"

    def test_exported_from_models(self):
        from agent_framework.models import CollectionStrategy
        assert CollectionStrategy.HYBRID == "HYBRID"


# ---------------------------------------------------------------------------
# Schema Args
# ---------------------------------------------------------------------------

class TestSchemaArgs:
    """SpawnAgentArgs and CheckSpawnResultArgs schema updates."""

    def test_spawn_agent_args_has_collection_strategy(self):
        from agent_framework.tools.schemas.builtin_args import SpawnAgentArgs
        args = SpawnAgentArgs(task_input="test", collection_strategy="BATCH_ALL", label="Agent A")
        assert args.collection_strategy == "BATCH_ALL"
        assert args.label == "Agent A"

    def test_spawn_agent_args_defaults(self):
        from agent_framework.tools.schemas.builtin_args import SpawnAgentArgs
        args = SpawnAgentArgs(task_input="test")
        assert args.collection_strategy == "HYBRID"
        assert args.label == ""

    def test_check_spawn_result_args_has_batch_pull(self):
        from agent_framework.tools.schemas.builtin_args import CheckSpawnResultArgs
        args = CheckSpawnResultArgs(batch_pull=True)
        assert args.batch_pull is True

    def test_check_spawn_result_args_defaults(self):
        from agent_framework.tools.schemas.builtin_args import CheckSpawnResultArgs
        args = CheckSpawnResultArgs()
        assert args.spawn_id == ""
        assert args.batch_pull is False


# ---------------------------------------------------------------------------
# ToolExecutor Integration
# ---------------------------------------------------------------------------

class TestToolExecutorCollectionIntegration:
    """ToolExecutor creates LeadCollector on async spawn."""

    def test_executor_has_lead_collector_slot(self):
        from agent_framework.tools.executor import ToolExecutor
        from unittest.mock import MagicMock
        executor = ToolExecutor(registry=MagicMock())
        assert executor._lead_collector is None

    def test_ensure_lead_collector_creates_on_first_call(self):
        from agent_framework.tools.executor import ToolExecutor
        from agent_framework.subagent.lead_collector import LeadCollector
        executor = ToolExecutor(registry=MagicMock())
        executor._ensure_lead_collector("HYBRID")
        assert isinstance(executor._lead_collector, LeadCollector)
        assert executor._lead_collector.strategy.value == "HYBRID"

    def test_ensure_lead_collector_idempotent(self):
        from agent_framework.tools.executor import ToolExecutor
        executor = ToolExecutor(registry=MagicMock())
        executor._ensure_lead_collector("SEQUENTIAL")
        first = executor._lead_collector
        executor._ensure_lead_collector("BATCH_ALL")  # Should NOT create new one
        assert executor._lead_collector is first

    def test_ensure_lead_collector_invalid_strategy_defaults_hybrid(self):
        from agent_framework.tools.executor import ToolExecutor
        executor = ToolExecutor(registry=MagicMock())
        executor._ensure_lead_collector("INVALID")
        assert executor._lead_collector.strategy.value == "HYBRID"

    def test_set_current_run_id_resets_lead_collector(self):
        """LeadCollector must be reset when a new run starts (no cross-run leakage)."""
        from agent_framework.tools.executor import ToolExecutor
        executor = ToolExecutor(registry=MagicMock())
        executor._ensure_lead_collector("HYBRID")
        executor._lead_collector.register_spawn("sp1", "task1")
        assert executor._lead_collector.total_spawned == 1

        # New run starts — collector must be reset
        executor.set_current_run_id("new_run_123")
        assert executor._lead_collector is None  # Fully cleared

    @pytest.mark.asyncio
    async def test_batch_pull_without_collector_returns_error(self):
        """batch_pull=true without prior async spawns returns explicit error."""
        from agent_framework.tools.executor import ToolExecutor
        executor = ToolExecutor(registry=MagicMock())
        # No _lead_collector set

        result = await executor._subagent_collect({"batch_pull": True})
        assert "error" in result
        assert result["total_spawned"] == 0


# ---------------------------------------------------------------------------
# Prompt Template
# ---------------------------------------------------------------------------

class TestPromptTemplateCollectionStrategy:
    """Orchestrator system prompt teaches collection strategies."""

    def test_prompt_mentions_collection_strategy(self):
        from agent_framework.agent.prompt_templates import ORCHESTRATOR_SYSTEM_PROMPT
        assert "collection_strategy" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "SEQUENTIAL" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "BATCH_ALL" in ORCHESTRATOR_SYSTEM_PROMPT
        assert "HYBRID" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_prompt_mentions_batch_pull(self):
        from agent_framework.agent.prompt_templates import ORCHESTRATOR_SYSTEM_PROMPT
        assert "batch_pull" in ORCHESTRATOR_SYSTEM_PROMPT

    def test_prompt_mentions_label(self):
        from agent_framework.agent.prompt_templates import ORCHESTRATOR_SYSTEM_PROMPT
        assert "label" in ORCHESTRATOR_SYSTEM_PROMPT

"""CE-* compliance tests — context engineering pluggability and strategies.

Covers CE-001 through CE-013 from context_engineering_spec.md §9.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.context.strategies import CompressionStrategy
from agent_framework.context.transaction_group import ToolTransactionGroup
from agent_framework.models.message import Message


def _counter(msgs: list[Message]) -> int:
    return sum(len(m.content or "") for m in msgs)


def _make_groups(n: int, chars: int = 100) -> list[ToolTransactionGroup]:
    return [
        ToolTransactionGroup(
            messages=[Message(role="user", content="x" * chars)],
            token_estimate=chars,
        )
        for _ in range(n)
    ]


# ── CE-001: ContextBuilderProtocol ──────────────────────────

class TestCE001_BuilderProtocol:
    def test_protocol_defined(self):
        from agent_framework.protocols.core import ContextBuilderProtocol
        assert hasattr(ContextBuilderProtocol, "calculate_tokens")
        assert hasattr(ContextBuilderProtocol, "set_token_budget")

    def test_default_builder_matches_protocol(self):
        from agent_framework.protocols.core import ContextBuilderProtocol
        builder = ContextBuilder()
        assert isinstance(builder, ContextBuilderProtocol)


# ── CE-002: ContextCompressorProtocol ────────────────────────

class TestCE002_CompressorProtocol:
    def test_protocol_defined(self):
        from agent_framework.protocols.core import ContextCompressorProtocol
        assert hasattr(ContextCompressorProtocol, "compress_groups_async")
        assert hasattr(ContextCompressorProtocol, "reset")

    def test_default_compressor_matches_protocol(self):
        from agent_framework.protocols.core import ContextCompressorProtocol
        comp = ContextCompressor()
        assert isinstance(comp, ContextCompressorProtocol)


# ── CE-003: ContextSourceProviderProtocol ────────────────────

class TestCE003_SourceProviderProtocol:
    def test_protocol_defined(self):
        from agent_framework.protocols.core import ContextSourceProviderProtocol
        assert hasattr(ContextSourceProviderProtocol, "collect_system_core")
        assert hasattr(ContextSourceProviderProtocol, "collect_saved_memory_block")

    def test_default_provider_matches_protocol(self):
        from agent_framework.protocols.core import ContextSourceProviderProtocol
        provider = ContextSourceProvider()
        assert isinstance(provider, ContextSourceProviderProtocol)


# ── CE-004: Compression strategy configurable ────────────────

class TestCE004_CompressionStrategy:
    def test_strategy_enum_values(self):
        assert CompressionStrategy.SUMMARIZATION == "SUMMARIZATION"
        assert CompressionStrategy.TRUNCATION == "TRUNCATION"
        assert CompressionStrategy.HYBRID == "HYBRID"
        assert CompressionStrategy.NONE == "NONE"

    def test_compressor_accepts_strategy(self):
        comp = ContextCompressor(strategy="TRUNCATION")
        assert comp._strategy == CompressionStrategy.TRUNCATION

    def test_compressor_default_is_summarization(self):
        comp = ContextCompressor()
        assert comp._strategy == CompressionStrategy.SUMMARIZATION

    @pytest.mark.asyncio
    async def test_none_strategy_returns_as_is(self):
        comp = ContextCompressor(token_counter=_counter, strategy="NONE")
        groups = _make_groups(5, chars=100)
        result = await comp.compress_groups_async(groups, target_tokens=50)
        assert len(result) == 5  # No compression

    @pytest.mark.asyncio
    async def test_truncation_drops_oldest(self):
        comp = ContextCompressor(token_counter=_counter, strategy="TRUNCATION")
        groups = _make_groups(5, chars=100)
        result = await comp.compress_groups_async(groups, target_tokens=250)
        assert len(result) < 5
        assert len(result) >= 2  # Protected groups preserved


# ── CE-005~007: Injectable components ────────────────────────

class TestCE005_007_Injectable:
    def test_engineer_accepts_custom_provider(self):
        from agent_framework.context.engineer import ContextEngineer

        class CustomProvider:
            def collect_system_core(self, *a, **kw): return []
            def collect_saved_memory_block(self, *a, **kw): return []
            def collect_session_groups(self, *a, **kw): return []
            def collect_skill_addon(self, *a, **kw): return []
            def collect_skill_catalog(self, *a, **kw): return []

        eng = ContextEngineer(source_provider=CustomProvider())
        assert eng._source is not None

    def test_engineer_accepts_custom_compressor(self):
        from agent_framework.context.engineer import ContextEngineer

        class CustomCompressor:
            async def compress_groups_async(self, *a, **kw): return []
            def reset(self): pass

        eng = ContextEngineer(compressor=CustomCompressor())
        assert eng._compressor is not None


# ── CE-009: Config class override ────────────────────────────

class TestCE009_ConfigOverride:
    def test_context_config_has_class_fields(self):
        from agent_framework.infra.config import ContextConfig
        cfg = ContextConfig()
        assert hasattr(cfg, "source_provider_class")
        assert hasattr(cfg, "compressor_class")
        assert hasattr(cfg, "builder_class")
        assert cfg.source_provider_class == ""
        assert cfg.compressor_class == ""
        assert cfg.builder_class == ""


# ── CE-010: Protected groups ─────────────────────────────────

class TestCE010_ProtectedGroups:
    @pytest.mark.asyncio
    async def test_truncation_preserves_last_2_groups(self):
        comp = ContextCompressor(token_counter=_counter, strategy="TRUNCATION")
        groups = _make_groups(10, chars=100)
        result = await comp.compress_groups_async(groups, target_tokens=250)
        # Last 2 groups always protected
        assert len(result) >= 2
        # The last two groups should be the original last two
        assert result[-1].messages[0].content == groups[-1].messages[0].content
        assert result[-2].messages[0].content == groups[-2].messages[0].content


# ── CE-011~012: Hook points ──────────────────────────────────

class TestCE011_012_Hooks:
    def test_context_hook_points_exist(self):
        from agent_framework.models.hook import HookPoint
        assert HookPoint.CONTEXT_PRE_BUILD == "context.pre_build"
        assert HookPoint.CONTEXT_POST_BUILD == "context.post_build"

    def test_pre_build_is_deniable(self):
        from agent_framework.models.hook import DENIABLE_HOOK_POINTS, HookPoint
        assert HookPoint.CONTEXT_PRE_BUILD in DENIABLE_HOOK_POINTS


# ── CE-013: Compressor fallback on error ─────────────────────

class TestCE013_Fallback:
    @pytest.mark.asyncio
    async def test_no_adapter_falls_back_to_truncation(self):
        comp = ContextCompressor(token_counter=_counter, strategy="SUMMARIZATION")
        groups = _make_groups(5, chars=100)
        # No adapter → fallback to truncation
        result = await comp.compress_groups_async(groups, target_tokens=250)
        assert len(result) < 5
        assert len(result) >= 2

    def test_reset_clears_state(self):
        comp = ContextCompressor()
        from agent_framework.context.compressor import SummaryBlock
        comp._frozen_summary = SummaryBlock(summary_text="test")
        comp.reset()
        assert comp._frozen_summary is None


# ── Spec §8: Error model ─────────────────────────────────────

class TestErrorModel:
    def test_context_budget_exceeded(self):
        from agent_framework.context.strategies import ContextBudgetExceeded
        exc = ContextBudgetExceeded(total_tokens=10000, budget_tokens=8192,
                                     strategy_used="SUMMARIZATION")
        assert exc.total_tokens == 10000
        assert exc.budget_tokens == 8192
        assert "10000/8192" in str(exc)

    def test_compression_error(self):
        from agent_framework.context.strategies import CompressionError
        exc = CompressionError(strategy="SUMMARIZATION",
                                original_error="LLM timeout")
        assert exc.strategy == "SUMMARIZATION"
        assert "LLM timeout" in str(exc)


# ── Spec §10 Scenario B: Strategy Switch ─────────────────────

class TestScenarioB_StrategySwitch:
    @pytest.mark.asyncio
    async def test_config_strategy_truncation(self):
        """config.context.default_compression_strategy = TRUNCATION → drops oldest."""
        comp = ContextCompressor(token_counter=_counter, strategy="TRUNCATION")
        groups = _make_groups(8, chars=100)
        result = await comp.compress_groups_async(groups, target_tokens=250)
        # Truncation: drops oldest, keeps last 2
        assert len(result) < 8
        assert len(result) >= 2

    @pytest.mark.asyncio
    async def test_config_strategy_none_returns_all(self):
        """NONE strategy returns all groups even if over budget."""
        comp = ContextCompressor(token_counter=_counter, strategy="NONE")
        groups = _make_groups(5, chars=100)
        result = await comp.compress_groups_async(groups, target_tokens=50)
        assert len(result) == 5


# ── Spec §10 Scenario C: Config Class Override ───────────────

class TestScenarioC_ConfigClassOverride:
    def test_entry_load_context_component_default(self):
        """Without class_path, _load_context_component uses default_factory."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework.__new__(AgentFramework)
        result = fw._load_context_component(
            class_path="",
            discovery_subdir="providers",
            class_suffix="Provider",
            default_factory=lambda: "default_instance",
        )
        assert result == "default_instance"

    def test_entry_load_context_component_invalid_class(self):
        """Invalid class_path falls back to default."""
        from agent_framework.entry import AgentFramework
        fw = AgentFramework.__new__(AgentFramework)
        result = fw._load_context_component(
            class_path="nonexistent.module.FakeClass",
            discovery_subdir="providers",
            class_suffix="Provider",
            default_factory=lambda: "fallback",
        )
        assert result == "fallback"

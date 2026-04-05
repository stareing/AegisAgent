"""Tests for OpenTelemetry tracing infrastructure.

Covers:
1. TracingManager noop behavior when disabled
2. TracingManager noop behavior when OTel SDK absent
3. Span context manager (sync and async)
4. TracingConfig defaults
5. _NoopSpan interface completeness
"""

from __future__ import annotations

import pytest

from agent_framework.infra.config import TracingConfig
from agent_framework.infra.telemetry import (_HAS_OTEL, _NOOP_SPAN,
                                             TracingManager, _NoopSpan)


class TestTracingConfig:
    def test_defaults(self) -> None:
        cfg = TracingConfig()
        assert cfg.enabled is False
        assert cfg.exporter_type == "otlp"
        assert cfg.service_name == "aegis-agent"

    def test_custom_values(self) -> None:
        cfg = TracingConfig(
            enabled=True,
            exporter_type="console",
            service_name="my-agent",
            otlp_endpoint="http://custom:4317",
        )
        assert cfg.enabled is True
        assert cfg.exporter_type == "console"


class TestNoopSpan:
    """_NoopSpan must silently accept all calls without error."""

    def test_set_attribute(self) -> None:
        _NOOP_SPAN.set_attribute("key", "value")

    def test_set_attributes(self) -> None:
        _NOOP_SPAN.set_attributes({"a": 1, "b": "two"})

    def test_add_event(self) -> None:
        _NOOP_SPAN.add_event("test.event", {"data": 123})

    def test_set_status(self) -> None:
        _NOOP_SPAN.set_status("OK")

    def test_record_exception(self) -> None:
        _NOOP_SPAN.record_exception(ValueError("test"))

    def test_end(self) -> None:
        _NOOP_SPAN.end()

    def test_context_manager(self) -> None:
        with _NOOP_SPAN as s:
            assert s is _NOOP_SPAN


class TestTracingManagerDisabled:
    """When disabled, TracingManager returns noop spans."""

    def test_disabled_by_default(self) -> None:
        tm = TracingManager()
        assert tm.enabled is False

    def test_configure_disabled(self) -> None:
        tm = TracingManager()
        tm.configure(TracingConfig(enabled=False))
        assert tm.enabled is False

    def test_span_returns_noop_when_disabled(self) -> None:
        tm = TracingManager()
        with tm.span("test.span") as s:
            assert isinstance(s, _NoopSpan)
            s.set_attribute("key", "value")
            s.add_event("test.event")

    @pytest.mark.asyncio
    async def test_async_span_returns_noop_when_disabled(self) -> None:
        tm = TracingManager()
        async with tm.async_span("test.span") as s:
            assert isinstance(s, _NoopSpan)

    def test_start_span_returns_noop_when_disabled(self) -> None:
        tm = TracingManager()
        span = tm.start_span("test.span")
        assert isinstance(span, _NoopSpan)
        span.end()

    def test_shutdown_noop_when_disabled(self) -> None:
        tm = TracingManager()
        tm.shutdown()  # Should not raise


class TestTracingManagerWithOTel:
    """Test with real OTel SDK if available."""

    @pytest.mark.skipif(not _HAS_OTEL, reason="opentelemetry-sdk not installed")
    def test_configure_console_exporter(self) -> None:
        tm = TracingManager()
        tm.configure(TracingConfig(enabled=True, exporter_type="console"))
        assert tm.enabled is True
        # Cleanup
        tm.shutdown()

    @pytest.mark.skipif(not _HAS_OTEL, reason="opentelemetry-sdk not installed")
    def test_span_creates_real_span(self) -> None:
        tm = TracingManager()
        tm.configure(TracingConfig(enabled=True, exporter_type="console"))
        try:
            with tm.span("test.real_span", attributes={"key": "value"}) as s:
                # Should be a real OTel span, not noop
                assert not isinstance(s, _NoopSpan)
                s.set_attribute("extra", "data")
                s.add_event("test.event")
        finally:
            tm.shutdown()

    @pytest.mark.skipif(not _HAS_OTEL, reason="opentelemetry-sdk not installed")
    @pytest.mark.asyncio
    async def test_async_span_creates_real_span(self) -> None:
        tm = TracingManager()
        tm.configure(TracingConfig(enabled=True, exporter_type="console"))
        try:
            async with tm.async_span("test.async_span") as s:
                assert not isinstance(s, _NoopSpan)
        finally:
            tm.shutdown()

    @pytest.mark.skipif(not _HAS_OTEL, reason="opentelemetry-sdk not installed")
    @pytest.mark.asyncio
    async def test_async_span_records_exception(self) -> None:
        tm = TracingManager()
        tm.configure(TracingConfig(enabled=True, exporter_type="console"))
        try:
            with pytest.raises(ValueError, match="boom"):
                async with tm.async_span("test.error_span") as s:
                    raise ValueError("boom")
        finally:
            tm.shutdown()

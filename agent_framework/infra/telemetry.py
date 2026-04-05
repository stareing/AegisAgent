"""OpenTelemetry tracing infrastructure — noop-safe.

If opentelemetry-api is not installed, all functions return noop objects
that silently ignore span creation. Business logic MUST NOT depend on
tracing — same architectural constraint as logger.py.

Span hierarchy:
    agent.run
    ├── agent.iteration[0]
    │   ├── agent.llm.call
    │   └── agent.tools.batch
    │       ├── agent.tool[read_file]
    │       └── agent.tool[spawn_agent]
    ├── agent.iteration[1]
    │   └── ...
    └── agent.memory.commit
"""

from __future__ import annotations

import contextlib
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any, Generator

from agent_framework.infra.config import TracingConfig

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


# ── OTel SDK detection ────────────────────────────────────────────────

try:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import (BatchSpanProcessor,
                                                ConsoleSpanExporter)
    from opentelemetry.trace import Span, StatusCode, Tracer

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False

# ── Noop fallbacks ────────────────────────────────────────────────────


class _NoopSpan:
    """Drop-in replacement for opentelemetry.trace.Span when SDK absent."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_attributes(self, attributes: dict[str, Any]) -> None:
        pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        pass

    def set_status(self, status: Any, description: str | None = None) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass

    def end(self) -> None:
        pass

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, *args: Any) -> None:
        pass


_NOOP_SPAN = _NoopSpan()


# ── TracingManager ────────────────────────────────────────────────────


class TracingManager:
    """Lazy-init tracing manager. Noop when OTel SDK is absent or disabled.

    Usage:
        tm = TracingManager()
        tm.configure(TracingConfig(enabled=True))

        with tm.span("agent.run", attributes={"run_id": "abc"}) as s:
            s.add_event("run.started")
            ...
    """

    def __init__(self) -> None:
        self._tracer: Any = None
        self._enabled = False

    def configure(self, config: TracingConfig) -> None:
        """Initialize OTel provider if enabled and SDK available."""
        if not config.enabled or not _HAS_OTEL:
            self._enabled = False
            return

        resource = Resource.create({"service.name": config.service_name})
        provider = TracerProvider(resource=resource)

        exporter = self._create_exporter(config)
        provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        self._tracer = trace.get_tracer("aegis-agent", "0.1.0")
        self._enabled = True

    @staticmethod
    def _create_exporter(config: TracingConfig) -> Any:
        """Build exporter based on config.exporter_type."""
        if config.exporter_type == "console":
            return ConsoleSpanExporter()

        if config.exporter_type == "otlp":
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import \
                    OTLPSpanExporter
                return OTLPSpanExporter(endpoint=config.otlp_endpoint)
            except ImportError:
                # Fallback to console if OTLP exporter not installed
                return ConsoleSpanExporter()

        # Default fallback
        return ConsoleSpanExporter()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @contextmanager
    def span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Generator[Any, None, None]:
        """Create a span context manager. Returns noop if disabled."""
        if not self._enabled or self._tracer is None:
            yield _NOOP_SPAN
            return

        with self._tracer.start_as_current_span(name, attributes=attributes) as s:
            yield s

    @asynccontextmanager
    async def async_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> AsyncGenerator[Any, None]:
        """Async span context manager. Returns noop if disabled."""
        if not self._enabled or self._tracer is None:
            yield _NOOP_SPAN
            return

        with self._tracer.start_as_current_span(name, attributes=attributes) as s:
            try:
                yield s
            except Exception as exc:
                s.record_exception(exc)
                s.set_status(StatusCode.ERROR, str(exc))
                raise

    def start_span(
        self,
        name: str,
        attributes: dict[str, Any] | None = None,
    ) -> Any:
        """Start a span manually (caller must call .end()). Returns noop if disabled."""
        if not self._enabled or self._tracer is None:
            return _NOOP_SPAN
        return self._tracer.start_span(name, attributes=attributes)

    def shutdown(self) -> None:
        """Flush and shut down the tracing provider."""
        if not self._enabled or not _HAS_OTEL:
            return
        provider = trace.get_tracer_provider()
        if hasattr(provider, "shutdown"):
            with contextlib.suppress(Exception):
                provider.shutdown()


# ── Module-level singleton ────────────────────────────────────────────

_manager = TracingManager()


def get_tracing_manager() -> TracingManager:
    """Return the module-level TracingManager singleton."""
    return _manager

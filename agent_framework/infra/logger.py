from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

import structlog

from agent_framework.infra.config import LoggingConfig

_configured = False

STANDARD_EVENTS = [
    # Run lifecycle
    "run.started",
    "run.finished",
    "run.failed",
    "run.skill_activated",
    # Iteration lifecycle
    "iteration.started",
    "iteration.completed",
    "iteration.stopped",
    "iteration.dispatching_tools",
    "iteration.tools_done",
    "iteration.no_tool_no_stop",
    # LLM
    "llm.calling",
    "llm.responded",
    "llm.error",
    "llm.error.forced_abort",
    "llm.error.continuing",
    # Tool execution
    "tool.routing",
    "tool.routing.mcp",
    "tool.routing.a2a",
    "tool.routing.subagent",
    "tool.routing.subagent.done",
    "tool.batch_executing",
    "tool.completed",
    "tool.failed",
    "tool.all_blocked",
    "tool.blocked_by_capability_policy",
    "tool.blocked",
    # Loop safety
    "loop.repeated_tool_calls_detected",
    # Context
    "context.compressed",
    # Memory
    "memory.saved",
    "memory.updated",
    "memory.deleted",
    # Delegation
    "delegation.subagent.requested",
    "delegation.subagent.approved",
    "delegation.subagent.hook_denied",
    "delegation.subagent.spawn_denied",
    "delegation.subagent.no_runtime",
    # SubAgent lifecycle
    "subagent.spawning",
    "subagent.creating",
    "subagent.created",
    "subagent.context_seed_built",
    "subagent.quota_check",
    "subagent.run_started",
    "subagent.run_finished",
    "subagent.spawn_completed",
    # Scheduler
    "scheduler.task_running",
    "scheduler.task_completed",
    "scheduler.task_timeout",
    "scheduler.task_cancelled",
    "scheduler.task_failed",
    "scheduler.quota_exceeded",
]


class StructLogger:
    """Structured logger wrapper that enforces standard fields.

    Standard fields:
    - timestamp (auto by structlog)
    - level (auto by structlog)
    - run_id
    - parent_run_id
    - spawn_id
    - iteration_index
    - event
    - duration_ms
    - error_code
    """

    def __init__(self, inner: structlog.stdlib.BoundLogger) -> None:
        self._inner = inner

    def bind(self, **kwargs: Any) -> StructLogger:
        """Bind standard fields to this logger instance."""
        return StructLogger(self._inner.bind(**kwargs))

    def bind_run(
        self,
        run_id: str,
        parent_run_id: str | None = None,
        spawn_id: str | None = None,
    ) -> StructLogger:
        """Bind run-level standard fields."""
        binds: dict[str, Any] = {"run_id": run_id}
        if parent_run_id:
            binds["parent_run_id"] = parent_run_id
        if spawn_id:
            binds["spawn_id"] = spawn_id
        return StructLogger(self._inner.bind(**binds))

    def bind_iteration(self, iteration_index: int) -> StructLogger:
        """Bind iteration-level standard fields."""
        return StructLogger(self._inner.bind(iteration_index=iteration_index))

    def info(self, event: str, **kwargs: Any) -> None:
        self._inner.info(event, **kwargs)

    def warning(self, event: str, **kwargs: Any) -> None:
        self._inner.warning(event, **kwargs)

    def error(
        self, event: str, error_code: str | None = None, **kwargs: Any
    ) -> None:
        if error_code:
            kwargs["error_code"] = error_code
        self._inner.error(event, **kwargs)

    def debug(self, event: str, **kwargs: Any) -> None:
        self._inner.debug(event, **kwargs)


def configure_logging(config: LoggingConfig | None = None) -> None:
    """Configure structlog with standard processors."""
    global _configured
    if _configured:
        return

    if config is None:
        config = LoggingConfig()

    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    level = getattr(logging, config.level.upper(), logging.INFO)

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if config.json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(level)

    file_handler = logging.FileHandler(log_dir / "agent.log")
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> StructLogger:
    """Get a StructLogger with standard field support."""
    if not _configured:
        configure_logging()
    return StructLogger(structlog.get_logger(name))

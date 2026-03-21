from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from agent_framework.agent.capability_policy import apply_capability_policy
from agent_framework.hooks.dispatcher import HookDispatchService
from agent_framework.hooks.errors import HookDeniedError
from agent_framework.hooks.payloads import (artifact_produced_payload,
                                            tool_error_payload,
                                            tool_post_use_payload,
                                            tool_pre_use_payload)
from agent_framework.infra.logger import get_logger
from agent_framework.infra.telemetry import get_tracing_manager
from agent_framework.models.agent import CapabilityPolicy
from agent_framework.models.hook import HookPoint
from agent_framework.models.message import Message, ToolCallRequest
from agent_framework.models.tool import (FieldError, ToolEntry,
                                         ToolExecutionError, ToolExecutionMeta,
                                         ToolResult)
from agent_framework.protocols.core import (ConfirmationHandlerProtocol,
                                            DelegationExecutorProtocol,
                                            ToolRegistryProtocol)
from agent_framework.tools.todo import TaskService

if TYPE_CHECKING:
    from agent_framework.models.message import Message
    from agent_framework.protocols.core import SubAgentRuntimeProtocol

logger = get_logger(__name__)

# Maximum number of events buffered in the child stream queue before
# backpressure kicks in.  TOKEN events (high volume, low priority) are
# dropped silently; all other event types force-drain the oldest items
# to make room.
_STREAM_QUEUE_MAX_SIZE: int = 1000

# Event types that are considered low-priority and can be dropped when the
# queue is full, rather than evicting existing items.
_LOW_PRIORITY_EVENT_TYPES: frozenset[str] = frozenset({"token", "subagent_stream"})

# Number of oldest items to drain when a high-priority event must be enqueued
# into a full queue.
_BACKPRESSURE_DRAIN_COUNT: int = 50


class ToolExecutor:
    """Executes tool calls with validation, routing, and error handling.

    CAPABILITY PLANE — sole execution entry point for Agent-facing tools.

    All external capabilities (local, MCP, A2A, subagent, memory_admin)
    MUST be invoked through execute() or batch_execute(). This ensures:
    - Capability policy enforcement (is_tool_allowed)
    - Confirmation handler (require_confirm)
    - Unified error envelope (ToolResult + ToolExecutionError)
    - Execution metadata (ToolExecutionMeta with timing/source)
    - Audit trail (structlog events)

    Direct calls to DelegationExecutor, MCPClientManager, or MemoryManager
    from the Agent run chain are PROHIBITED — they bypass security and audit.
    Admin-plane methods on AgentFramework (entry.py) are the only exception.

    Routing rules:
    - local    -> _route_local (direct function call)
    - mcp      -> _route_mcp (MCPClientManager.call_mcp_tool)
    - a2a      -> _route_a2a (DelegationExecutor.delegate_to_a2a)
    - subagent -> _route_subagent (_subagent_spawn / _subagent_collect)
    """

    def __init__(
        self,
        registry: ToolRegistryProtocol,
        confirmation_handler: ConfirmationHandlerProtocol | None = None,
        delegation_executor: DelegationExecutorProtocol | None = None,
        mcp_client_manager: Any = None,
        parent_agent_getter: Callable[[], Any | None] | None = None,
        max_concurrent: int = 5,
        hook_executor: Any = None,
        default_collection_strategy: str = "HYBRID",
        collection_poll_interval_ms: int = 500,
    ) -> None:
        self._registry = registry
        self._confirmation = confirmation_handler
        self._delegation = delegation_executor
        self._mcp = mcp_client_manager
        self._parent_agent_getter = parent_agent_getter
        self._max_concurrent = max_concurrent
        self._hook_executor = hook_executor
        self._hook_dispatcher: HookDispatchService | None = (
            HookDispatchService(hook_executor) if hook_executor is not None else None
        )
        # Set by RunCoordinator at run start — used for parent_run_id in spawn
        self._current_run_id: str = ""
        # Set by RunCoordinator each iteration — used for child context seed.
        self._current_session_messages: list[Message] = []
        # Set by RunCoordinator from EffectiveRunConfig
        self._progressive_mode: bool = False
        # Run-scoped task graph management
        self._todo_service = TaskService()
        # Collection strategy config defaults (from SubAgentConfig)
        self._default_collection_strategy: str = default_collection_strategy.upper()
        self._collection_poll_interval_ms: int = collection_poll_interval_ms
        # Lead collector for multi-agent result collection strategies
        self._lead_collector: Any = None  # LeadCollector, created on first async spawn
        # Queue for real-time sub-agent stream events (TOKEN, TOOL_CALL, etc.)
        # Filled by SubAgentRuntime._stream_sink, drained by batch_execute_progressive.
        # Bounded to _STREAM_QUEUE_MAX_SIZE to prevent unbounded memory growth;
        # backpressure logic lives in enqueue_child_stream_event().
        self._child_stream_queue: asyncio.Queue = asyncio.Queue(
            maxsize=_STREAM_QUEUE_MAX_SIZE
        )

    @property
    def todo_service(self) -> TaskService:
        """Expose TaskService for coordinator to read task state."""
        return self._todo_service

    def set_current_run_id(self, run_id: str) -> None:
        """Called by RunCoordinator to bind the current run_id for quota tracking.

        Also resets the LeadCollector so spawn tracking from previous runs
        does not leak into the new run.
        """
        self._current_run_id = run_id
        # Reset per-run state: LeadCollector must not carry over spawns from prior runs
        if self._lead_collector is not None:
            self._lead_collector.reset()
            self._lead_collector = None

    def set_current_session_messages(self, messages: list[Message]) -> None:
        """Called by RunCoordinator before each iteration for spawn seed building."""
        self._current_session_messages = list(messages or [])

    # ------------------------------------------------------------------
    # Backpressure-aware child stream enqueue
    # ------------------------------------------------------------------

    def enqueue_child_stream_event(self, event: Any) -> None:
        """Put a StreamEvent into the child stream queue with backpressure.

        Policy:
        - If the queue has room, enqueue unconditionally.
        - If full and the event carries a low-priority event_type (e.g. TOKEN),
          drop it silently and log at debug level.
        - If full and the event is high-priority (TOOL_CALL_START, TOOL_CALL_DONE,
          ITERATION_START, etc.), drain the oldest items to make room.
        """
        if not self._child_stream_queue.full():
            self._child_stream_queue.put_nowait(event)
            return

        # Determine the inner event_type carried in the wrapper
        inner_type = self._extract_event_type(event)

        if inner_type in _LOW_PRIORITY_EVENT_TYPES:
            logger.debug(
                "stream_queue.backpressure.drop",
                event_type=inner_type,
                queue_size=self._child_stream_queue.qsize(),
            )
            return

        # High-priority event — force-drain oldest items to make room
        drained = 0
        while drained < _BACKPRESSURE_DRAIN_COUNT and not self._child_stream_queue.empty():
            try:
                self._child_stream_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break

        logger.debug(
            "stream_queue.backpressure.drain",
            event_type=inner_type,
            drained=drained,
        )
        self._child_stream_queue.put_nowait(event)

    @staticmethod
    def _extract_event_type(event: Any) -> str:
        """Extract the logical event type string from a StreamEvent.

        For SUBAGENT_STREAM wrappers the inner event_type lives in data;
        otherwise we use the top-level type value.
        """
        if hasattr(event, "data") and isinstance(event.data, dict):
            inner = event.data.get("event_type")
            if inner is not None:
                return str(inner)
        if hasattr(event, "type"):
            return str(event.type.value) if hasattr(event.type, "value") else str(event.type)
        return ""

    async def execute(
        self, tool_call_request: ToolCallRequest, policy: CapabilityPolicy | None = None
    ) -> tuple[ToolResult, ToolExecutionMeta]:
        _tm = get_tracing_manager()
        start = time.monotonic()
        tool_name = tool_call_request.function_name

        # Lookup
        if not self._registry.has_tool(tool_name):
            return self._not_found(tool_call_request, start)

        entry = self._registry.get_tool(tool_name)

        # Capability policy enforcement
        if policy is not None and not self.is_tool_allowed(tool_name, policy):
            return self._permission_denied(tool_call_request, start)

        # Confirmation — decision is policy + tool metadata, handler only executes flow
        if self._should_confirm(entry) and self._confirmation:
            approved = await self._confirmation.request_confirmation(
                tool_name, tool_call_request.arguments, entry.meta.description
            )
            if not approved:
                return self._permission_denied(tool_call_request, start)

        # Validate
        validated = self._validate_arguments(entry, tool_call_request.arguments)
        if isinstance(validated, ToolExecutionError):
            return (
                ToolResult(
                    tool_call_id=tool_call_request.id,
                    tool_name=tool_name,
                    success=False,
                    error=validated,
                ),
                self._meta(entry, start),
            )

        # PRE_TOOL_USE hook
        if self._hook_dispatcher is not None:
            try:
                outcome = await self._hook_dispatcher.fire(
                    HookPoint.PRE_TOOL_USE,
                    run_id=self._current_run_id or None,
                    payload=tool_pre_use_payload(
                        tool_name=tool_name,
                        tool_call_id=tool_call_request.id or "",
                        arguments=validated,
                        tool_tags=list(entry.meta.tags),
                        source=entry.meta.source,
                    ),
                )
                if outcome.needs_confirmation and self._confirmation:
                    approved = await self._confirmation.request_confirmation(
                        tool_name, validated,
                        outcome.confirmation_reason,
                    )
                    if not approved:
                        return self._permission_denied(tool_call_request, start)
                if "sanitized_arguments" in outcome.modifications:
                    validated = outcome.modifications["sanitized_arguments"]
            except HookDeniedError as hde:
                logger.info("tool.hook_denied", tool_name=tool_name, reason=str(hde))
                return self._permission_denied(tool_call_request, start)

        # Execute
        with _tm.span("agent.tool", attributes={
            "tool_name": tool_name,
            "tool_call_id": tool_call_request.id or "",
            "source": entry.meta.source if hasattr(entry.meta, "source") else "local",
        }) as _tool_span:
            try:
                output = await self._route_execution(entry, validated)
                output = self._sanitize_output(output, tool_name)
                _tool_span.set_attribute("success", True)
                result = ToolResult(
                    tool_call_id=tool_call_request.id,
                    tool_name=tool_name,
                    success=True,
                    output=output,
                )
                meta = self._meta(entry, start)
                # POST_TOOL_USE hook (best-effort)
                if self._hook_dispatcher is not None:
                    try:
                        post_outcome = await self._hook_dispatcher.fire(
                            HookPoint.POST_TOOL_USE,
                            run_id=self._current_run_id or None,
                            payload=tool_post_use_payload(
                                tool_name=tool_name,
                                tool_call_id=tool_call_request.id or "",
                                success=True,
                                output_preview=str(output),
                            ),
                        )
                        for art in post_outcome.emitted_artifacts:
                            await self._hook_dispatcher.fire_advisory(
                                HookPoint.ARTIFACT_PRODUCED,
                                run_id=self._current_run_id or None,
                                payload=artifact_produced_payload(art, tool_name),
                            )
                    except Exception:
                        pass
                return result, meta
            except Exception as e:
                _tool_span.set_attribute("success", False)
                _tool_span.record_exception(e)
                if self._hook_dispatcher is not None:
                    await self._hook_dispatcher.fire_advisory(
                        HookPoint.TOOL_ERROR,
                        run_id=self._current_run_id or None,
                        payload=tool_error_payload(tool_name, type(e).__name__, str(e)),
                    )
                return self._handle_tool_error(tool_name, e, entry, start, tool_call_id=tool_call_request.id)

    async def batch_execute(
        self, tool_call_requests: list[ToolCallRequest], policy: CapabilityPolicy | None = None
    ) -> list[tuple[ToolResult, ToolExecutionMeta]]:
        """Execute multiple tool calls concurrently with bounded parallelism.

        Order guarantee:
        - Results are returned in the SAME ORDER as input tool_call_requests.
        - The i-th result corresponds to the i-th request, regardless of
          which tool finishes first.
        - This is enforced by asyncio.gather() which preserves positional order.
        - Downstream code (session projection, debug, tests) relies on this
          stability — do NOT change to completion-order collection.

        Side-effect commit boundary (v2.6.4 §43):
        - Concurrent execution only covers the COMPUTATION phase.
        - Observable side effects (session writes, artifact registration,
          audit records) are NOT committed by tool threads directly.
        - ToolExecutor collects results; RunStateController commits them
          in input order via ToolCommitSequencer.
        - Tool threads MUST NOT write SessionState, register artifacts,
          or submit audit records directly.
        """
        sem = asyncio.Semaphore(self._max_concurrent)

        async def _run(req: ToolCallRequest) -> tuple[ToolResult, ToolExecutionMeta]:
            async with sem:
                return await self.execute(req, policy=policy)

        return await asyncio.gather(*[_run(r) for r in tool_call_requests])

    async def batch_execute_progressive(
        self, tool_call_requests: list[ToolCallRequest], policy: CapabilityPolicy | None = None
    ) -> AsyncIterator[tuple[ToolResult, ToolExecutionMeta] | Any]:
        """Execute concurrently, yield results as each tool completes.

        Also drains _child_stream_queue in real-time, yielding StreamEvent
        objects interleaved with (ToolResult, ToolExecutionMeta) tuples.
        Consumers must check the type of each yielded item.

        Uses asyncio.wait on both queues simultaneously to avoid
        idle polling when no child stream events are present.
        """
        from agent_framework.models.stream import StreamEvent

        sem = asyncio.Semaphore(self._max_concurrent)
        done_queue: asyncio.Queue[tuple[ToolResult, ToolExecutionMeta]] = asyncio.Queue()

        async def _run(req: ToolCallRequest) -> None:
            async with sem:
                result = await self.execute(req, policy=policy)
            await done_queue.put(result)

        tasks = [asyncio.create_task(_run(r)) for r in tool_call_requests]
        remaining = len(tasks)
        has_child_stream = self._child_stream_queue.maxsize > 0

        while remaining > 0:
            # Create competing wait tasks for both queues
            done_waiter = asyncio.create_task(done_queue.get())
            child_waiter = asyncio.create_task(
                self._child_stream_queue.get()
            ) if has_child_stream else None

            pending_futures = {done_waiter}
            if child_waiter:
                pending_futures.add(child_waiter)

            # Wait for EITHER a tool completion OR a child stream event
            finished, _ = await asyncio.wait(
                pending_futures, return_when=asyncio.FIRST_COMPLETED,
            )

            for fut in finished:
                if fut is done_waiter:
                    remaining -= 1
                    yield fut.result()
                elif fut is child_waiter:
                    event = fut.result()
                    if isinstance(event, StreamEvent):
                        yield event
                    # Re-arm child waiter in next iteration
                    child_waiter = None

            # Cancel unfinished waiters
            for fut in pending_futures - finished:
                fut.cancel()
                try:
                    await fut
                except (asyncio.CancelledError, Exception):
                    pass

            # Drain any additional child events accumulated during tool execution
            while not self._child_stream_queue.empty():
                child_event = self._child_stream_queue.get_nowait()
                if isinstance(child_event, StreamEvent):
                    yield child_event

        # Final drain after all tools complete
        while not self._child_stream_queue.empty():
            child_event = self._child_stream_queue.get_nowait()
            if isinstance(child_event, StreamEvent):
                yield child_event

    def is_tool_allowed(self, tool_name: str, policy: CapabilityPolicy) -> bool:
        """Hard runtime gate for tool permission ceiling.

        This is the SECURITY BOUNDARY — schema-level filtering (export_schemas)
        is a visibility optimization, NOT a security check. Execution-time
        validation here is the authoritative enforcement point.
        """
        if not self._registry.has_tool(tool_name):
            return False
        allowed = apply_capability_policy(self._registry.list_tools(), policy)
        allowed_names = {t.meta.name for t in allowed}
        return tool_name in allowed_names

    def _validate_arguments(
        self, tool_entry: ToolEntry, arguments: dict
    ) -> dict | ToolExecutionError:
        if tool_entry.validator_model is None:
            return arguments
        try:
            obj = tool_entry.validator_model(**arguments)
            return obj.model_dump()
        except ValidationError as e:
            field_errors = []
            for err in e.errors():
                loc = ".".join(str(x) for x in err["loc"])
                field_errors.append(
                    FieldError(
                        field=loc,
                        message=err["msg"],
                        expected=err.get("type"),
                        received=str(err.get("input", "")),
                    )
                )
            return ToolExecutionError(
                error_type="VALIDATION_ERROR",
                error_code="INVALID_ARGUMENT_TYPE",
                message=str(e),
                field_errors=field_errors,
                retryable=True,
            )

    async def _route_execution(self, tool_entry: ToolEntry, validated_arguments: dict) -> Any:
        """Route to appropriate executor based on tool source.

        Pure dispatch — each source has its own _route_* method.
        """
        source = tool_entry.meta.source
        logger.info(
            "tool.routing",
            tool_name=tool_entry.meta.name,
            source=source,
            arguments_keys=list(validated_arguments.keys()),
        )

        router = {
            "local": self._route_local,
            "mcp": self._route_mcp,
            "a2a": self._route_a2a,
            "subagent": self._route_subagent,
        }
        handler = router.get(source)
        if handler is None:
            raise RuntimeError(f"Unknown tool source: {source}")
        return await handler(tool_entry, validated_arguments)

    # ── Source-specific routing ───────────────────────────────

    # ── Task graph tool names (run-scoped interception) ─────────
    _TASK_TOOLS = frozenset({"task_create", "task_update", "task_list", "task_get"})

    async def _route_local(self, entry: ToolEntry, args: dict) -> Any:
        # Run-scoped task graph interception
        if entry.meta.name in self._TASK_TOOLS and self._current_run_id:
            mgr = self._todo_service.get(self._current_run_id)
            name = entry.meta.name
            if name == "task_create":
                return mgr.create(
                    args.get("subject", ""),
                    args.get("description", ""),
                    args.get("blocked_by"),
                    args.get("active_form", ""),
                    args.get("metadata"),
                )
            elif name == "task_update":
                return mgr.update(
                    args["task_id"],
                    status=args.get("status"),
                    subject=args.get("subject"),
                    description=args.get("description"),
                    active_form=args.get("active_form"),
                    add_blocked_by=args.get("add_blocked_by"),
                    add_blocks=args.get("add_blocks"),
                    owner=args.get("owner"),
                    metadata=args.get("metadata"),
                )
            elif name == "task_list":
                return mgr.list_all()
            elif name == "task_get":
                return mgr.get(args["task_id"])

        if entry.callable_ref is None:
            raise RuntimeError(f"No callable for tool {entry.meta.name}")
        if entry.meta.is_async:
            return await entry.callable_ref(**args)
        return await asyncio.to_thread(entry.callable_ref, **args)

    async def _route_mcp(self, entry: ToolEntry, args: dict) -> Any:
        if self._mcp is None:
            raise RuntimeError("MCPClientManager not configured")
        server_id = entry.meta.mcp_server_id
        logger.info("tool.routing.mcp", tool_name=entry.meta.name, server_id=server_id)
        return await self._mcp.call_mcp_tool(server_id, entry.meta.name, args)

    async def _route_a2a(self, entry: ToolEntry, args: dict) -> Any:
        if self._delegation is None:
            raise RuntimeError("DelegationExecutor not configured")
        agent_url = entry.meta.a2a_agent_url or ""
        logger.info("tool.routing.a2a", tool_name=entry.meta.name, agent_url=agent_url)
        result = await self._delegation.delegate_to_a2a(
            agent_url=agent_url,
            task_input=str(args.get("task_input", "")),
            skill_id=args.get("skill_id"),
        )
        # Unified delegation summary — same protocol as local subagent
        from agent_framework.subagent.delegation import DelegationExecutor
        return DelegationExecutor.summarize_result(result).model_dump()

    async def _route_subagent(self, entry: ToolEntry, args: dict) -> Any:
        # Team tools — routed before delegation check (team may work without delegation)
        if entry.meta.name == "team":
            from agent_framework.tools.builtin.team_tools import execute_team
            return await execute_team(self, args)
        if entry.meta.name == "mail":
            from agent_framework.tools.builtin.team_tools import execute_mail
            return await execute_mail(self, args)

        if self._delegation is None:
            raise RuntimeError("DelegationExecutor not configured")

        if entry.meta.name == "check_spawn_result":
            return await self._subagent_collect(args)
        if entry.meta.name == "send_message":
            return await self._subagent_send_message(args)
        if entry.meta.name == "close_agent":
            return await self._subagent_close(args)
        if entry.meta.name == "resume_checkpoint":
            return await self._subagent_resume_checkpoint(args)
        return await self._subagent_spawn(args)

    # ── Sub-agent operations ─────────────────────────────────

    async def _subagent_spawn(self, args: dict) -> Any:
        """Build SubAgentSpec from arguments and dispatch sync or async."""
        parent_run_id = self._current_run_id
        from agent_framework.tools.builtin.spawn_agent import execute_spawn_agent
        return await execute_spawn_agent(self, args)

    async def _subagent_collect(self, args: dict) -> dict:
        """Collect async sub-agent result — single or batch."""
        from agent_framework.tools.builtin.spawn_agent import \
            execute_check_spawn_result
        return await execute_check_spawn_result(self, args)

    async def _subagent_send_message(self, args: dict) -> dict:
        """Send a message to a LONG_LIVED sub-agent."""
        from agent_framework.tools.builtin.spawn_agent import execute_send_message
        return await execute_send_message(self, args)

    async def _subagent_close(self, args: dict) -> dict:
        """Close a LONG_LIVED sub-agent."""
        from agent_framework.tools.builtin.spawn_agent import execute_close_agent
        return await execute_close_agent(self, args)

    async def _subagent_resume_checkpoint(self, args: dict) -> dict:
        """Resume a sub-agent from a saved checkpoint."""
        from agent_framework.tools.builtin.spawn_agent import execute_resume_checkpoint
        return await execute_resume_checkpoint(self, args)

    def _ensure_lead_collector(self, strategy_str: str) -> None:
        """Create LeadCollector on first async spawn if not exists.

        Falls back to config default when strategy_str is empty or invalid.
        Uses config-driven poll_interval_ms for SEQUENTIAL/HYBRID polling.

        If collector already exists and a different strategy is requested,
        logs a warning — the first spawn's strategy wins for the entire run.
        """
        from agent_framework.tools.builtin.spawn_agent import \
            ensure_lead_collector
        ensure_lead_collector(self, strategy_str)

    def _handle_tool_error(
        self, tool_name: str, error: Exception, entry: ToolEntry | None = None, start: float = 0.0, tool_call_id: str = ""
    ) -> tuple[ToolResult, ToolExecutionMeta]:
        logger.error(
            "tool.failed",
            tool_name=tool_name,
            error=str(error),
        )
        source = entry.meta.source if entry else "local"
        return (
            ToolResult(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                success=False,
                error=ToolExecutionError(
                    error_type="EXECUTION_ERROR",
                    error_code="RUNTIME_ERROR",
                    message=str(error),
                    retryable=False,
                ),
            ),
            ToolExecutionMeta(
                execution_time_ms=int((time.monotonic() - start) * 1000),
                source=source,
            ),
        )

    def _not_found(
        self, req: ToolCallRequest, start: float
    ) -> tuple[ToolResult, ToolExecutionMeta]:
        return (
            ToolResult(
                tool_call_id=req.id,
                tool_name=req.function_name,
                success=False,
                error=ToolExecutionError(
                    error_type="NOT_FOUND",
                    error_code="TOOL_NOT_FOUND",
                    message=f"Tool '{req.function_name}' not found",
                    retryable=False,
                ),
            ),
            ToolExecutionMeta(
                execution_time_ms=int((time.monotonic() - start) * 1000),
                source="local",
            ),
        )

    def _permission_denied(
        self, req: ToolCallRequest, start: float
    ) -> tuple[ToolResult, ToolExecutionMeta]:
        return (
            ToolResult(
                tool_call_id=req.id,
                tool_name=req.function_name,
                success=False,
                error=ToolExecutionError(
                    error_type="PERMISSION_DENIED",
                    error_code="USER_DENIED",
                    message="User denied tool execution",
                    retryable=False,
                ),
            ),
            ToolExecutionMeta(
                execution_time_ms=int((time.monotonic() - start) * 1000),
                source="local",
            ),
        )

    # ------------------------------------------------------------------
    # Output sanitization — enforces JSON-serializable boundary (#9)
    # ------------------------------------------------------------------

    # Maximum output size in characters before truncation
    _MAX_OUTPUT_CHARS = 50_000

    @staticmethod
    def _sanitize_output(output: Any, tool_name: str) -> Any:
        """Ensure tool output is JSON-serializable and bounded in size.

        Contract:
        - output must be JSON-serializable (str, int, float, bool, None, dict, list).
        - Callables, connection objects, exception objects are coerced to str.
        - Large outputs are truncated with a warning suffix.
        - This is the LAST gate before output enters ToolResult and gets
          projected into SessionState / LLM context.
        """
        import json

        # Fast path: primitives
        if output is None or isinstance(output, (str, int, float, bool)):
            if isinstance(output, str) and len(output) > ToolExecutor._MAX_OUTPUT_CHARS:
                return output[: ToolExecutor._MAX_OUTPUT_CHARS] + f"\n... [truncated, tool={tool_name}]"
            return output

        # Try JSON serialization to validate
        try:
            json.dumps(output, default=str)
        except (TypeError, ValueError):
            logger.warning(
                "tool.output_not_serializable",
                tool_name=tool_name,
                output_type=type(output).__name__,
            )
            output = str(output)

        # Size check for serialized form
        if isinstance(output, (dict, list)):
            serialized = json.dumps(output, default=str, ensure_ascii=False)
            if len(serialized) > ToolExecutor._MAX_OUTPUT_CHARS:
                logger.warning(
                    "tool.output_truncated",
                    tool_name=tool_name,
                    original_chars=len(serialized),
                    limit=ToolExecutor._MAX_OUTPUT_CHARS,
                )
                return serialized[: ToolExecutor._MAX_OUTPUT_CHARS] + f"\n... [truncated, tool={tool_name}]"

        return output

    # ------------------------------------------------------------------
    # Confirmation decision (#12) — policy can escalate confirmation
    # ------------------------------------------------------------------

    def _should_confirm(
        self, entry: ToolEntry, policy: CapabilityPolicy | None = None
    ) -> bool:
        """Determine if tool execution requires user confirmation.

        Decision hierarchy:
        1. CapabilityPolicy.force_confirm_categories → always confirm tools in these categories
        2. ToolMeta.require_confirm=True → tool-level declaration
        3. Default: no confirmation required

        The ConfirmationHandler only executes the confirmation flow.
        The decision of WHETHER to confirm lives here.
        """
        # Tool-level declaration
        if entry.meta.require_confirm:
            return True

        # Policy-level escalation
        if policy is not None:
            force_categories = getattr(policy, "force_confirm_categories", None)
            if force_categories and entry.meta.category in force_categories:
                return True

        return False

    def _meta(self, entry: ToolEntry, start: float) -> ToolExecutionMeta:
        return ToolExecutionMeta(
            execution_time_ms=int((time.monotonic() - start) * 1000),
            source=entry.meta.source,
        )

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from agent_framework.infra.logger import get_logger
from agent_framework.infra.telemetry import get_tracing_manager
from agent_framework.models.hook import HookPoint
from agent_framework.hooks.errors import HookDeniedError
from agent_framework.hooks.dispatcher import HookDispatchService
from agent_framework.hooks.payloads import (
    tool_pre_use_payload, tool_post_use_payload, tool_error_payload,
    artifact_produced_payload,
)
from agent_framework.agent.capability_policy import apply_capability_policy
from agent_framework.models.agent import CapabilityPolicy
from agent_framework.models.message import Message, ToolCallRequest
from agent_framework.models.tool import (
    FieldError,
    ToolEntry,
    ToolExecutionError,
    ToolExecutionMeta,
    ToolResult,
)
from agent_framework.protocols.core import (
    ConfirmationHandlerProtocol,
    DelegationExecutorProtocol,
    ToolRegistryProtocol,
)
from agent_framework.tools.todo import TaskService

if TYPE_CHECKING:
    from agent_framework.models.message import Message
    from agent_framework.protocols.core import SubAgentRuntimeProtocol

logger = get_logger(__name__)


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

    @property
    def todo_service(self) -> TaskService:
        """Expose TaskService for coordinator to read task state."""
        return self._todo_service

    def set_current_run_id(self, run_id: str) -> None:
        """Called by RunCoordinator to bind the current run_id for quota tracking."""
        self._current_run_id = run_id

    def set_current_session_messages(self, messages: list[Message]) -> None:
        """Called by RunCoordinator before each iteration for spawn seed building."""
        self._current_session_messages = list(messages or [])

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
    ) -> AsyncIterator[tuple[ToolResult, ToolExecutionMeta]]:
        """Execute concurrently, yield results as each tool completes (fastest first).

        Unlike batch_execute which waits for all, this yields each result
        immediately upon completion. Order is by completion time, not input order.
        """
        sem = asyncio.Semaphore(self._max_concurrent)

        async def _run(req: ToolCallRequest) -> tuple[ToolResult, ToolExecutionMeta]:
            async with sem:
                return await self.execute(req, policy=policy)

        tasks = {asyncio.create_task(_run(r)): r for r in tool_call_requests}
        for coro in asyncio.as_completed(tasks.keys()):
            yield await coro

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
                )
            elif name == "task_update":
                return mgr.update(
                    args["task_id"],
                    status=args.get("status"),
                    subject=args.get("subject"),
                    description=args.get("description"),
                    add_blocked_by=args.get("add_blocked_by"),
                    add_blocks=args.get("add_blocks"),
                    owner=args.get("owner"),
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
        from agent_framework.tools.delegation import DelegationExecutor
        return DelegationExecutor.summarize_result(result).model_dump()

    async def _route_subagent(self, entry: ToolEntry, args: dict) -> Any:
        if self._delegation is None:
            raise RuntimeError("DelegationExecutor not configured")

        if entry.meta.name == "check_spawn_result":
            return await self._subagent_collect(args)
        return await self._subagent_spawn(args)

    # ── Sub-agent operations ─────────────────────────────────

    async def _subagent_spawn(self, args: dict) -> Any:
        """Build SubAgentSpec from arguments and dispatch sync or async."""
        from agent_framework.models.subagent import (
            MemoryScope, SpawnContextMode, SpawnMode, SubAgentSpec,
        )
        from agent_framework.context.builder import ContextBuilder

        mode_str = args.get("mode", "ephemeral").upper()
        scope_str = args.get("memory_scope", "isolated").upper()
        context_mode_str = args.get("context_mode", "minimal").upper()
        wait = args.get("wait", True)
        # In progressive mode, force wait=True — the batch_execute_progressive
        # already handles "return as each completes". Using wait=False would
        # make spawn instant (just returns spawn_id) defeating progressive's purpose.
        if self._progressive_mode:
            wait = True
        parent_agent = self._parent_agent_getter() if self._parent_agent_getter else None
        parent_run_id = self._current_run_id
        if not parent_run_id and parent_agent and hasattr(parent_agent, "agent_id"):
            parent_run_id = parent_agent.agent_id

        context_mode = (
            SpawnContextMode(context_mode_str)
            if context_mode_str in SpawnContextMode.__members__
            else SpawnContextMode.MINIMAL
        )

        spec = SubAgentSpec(
            parent_run_id=parent_run_id,
            task_input=args.get("task_input", ""),
            mode=SpawnMode(mode_str) if mode_str in SpawnMode.__members__ else SpawnMode.EPHEMERAL,
            skill_id=args.get("skill_id"),
            tool_category_whitelist=args.get("tool_categories"),
            context_mode=context_mode,
            memory_scope=MemoryScope(scope_str) if scope_str in MemoryScope.__members__ else MemoryScope.ISOLATED,
            token_budget=int(args.get("token_budget", 4096)),
            max_iterations=int(args.get("max_iterations", 10)),
            deadline_ms=int(args.get("deadline_ms", 0)),
        )
        if spec.context_seed is None:
            builder = ContextBuilder()
            if context_mode == SpawnContextMode.MINIMAL:
                # MINIMAL: only the task_input — prevents sibling task leakage
                spec.context_seed = [Message(role="user", content=spec.task_input)]
            else:
                # PARENT_CONTEXT: filtered parent session (no tool/delegation messages)
                spec.context_seed = builder.build_filtered_spawn_seed(
                    session_messages=self._current_session_messages,
                    query=spec.task_input,
                    token_budget=spec.token_budget,
                )

        parent_id = getattr(parent_agent, "agent_id", "unknown") if parent_agent else "none"
        logger.info(
            "tool.routing.subagent",
            task_input=spec.task_input[:150],
            mode=mode_str,
            memory_scope=scope_str,
            wait=wait,
            parent_agent_id=parent_id,
        )

        if not wait:
            spawn_id = await self._delegation.delegate_to_subagent_async(spec, parent_agent)
            logger.info("tool.routing.subagent.async_submitted", spawn_id=spawn_id)
            return {
                "spawn_id": spawn_id,
                "status": "PENDING",
                "message": "Sub-agent started asynchronously. Use check_spawn_result to collect the result.",
            }

        result = await self._delegation.delegate_to_subagent(spec, parent_agent)
        logger.info(
            "tool.routing.subagent.done",
            spawn_id=result.spawn_id,
            success=result.success,
            iterations_used=result.iterations_used,
            answer_preview=(result.final_answer or result.error or "")[:120],
        )
        from agent_framework.tools.delegation import DelegationExecutor
        return DelegationExecutor.summarize_result(result).model_dump()

    async def _subagent_collect(self, args: dict) -> dict:
        """Collect async sub-agent result by spawn_id."""
        spawn_id = args.get("spawn_id", "")
        wait = args.get("wait", True)
        result = await self._delegation.collect_subagent_result(spawn_id, wait=wait)
        if result is None:
            return {"spawn_id": spawn_id, "status": "RUNNING"}
        from agent_framework.tools.delegation import DelegationExecutor
        return DelegationExecutor.summarize_result(result).model_dump()

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

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Callable

from pydantic import ValidationError

from agent_framework.infra.logger import get_logger
from agent_framework.agent.capability_policy import apply_capability_policy
from agent_framework.models.agent import CapabilityPolicy
from agent_framework.models.message import ToolCallRequest
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

if TYPE_CHECKING:
    from agent_framework.protocols.core import SubAgentRuntimeProtocol

logger = get_logger(__name__)


class ToolExecutor:
    """Executes tool calls with validation, routing, and error handling.

    Routing rules (section 10.5):
    - local -> local function call
    - mcp -> MCPClientManager.call_mcp_tool()
    - a2a -> DelegationExecutor.delegate_to_a2a()
    - subagent -> DelegationExecutor.delegate_to_subagent()
    """

    def __init__(
        self,
        registry: ToolRegistryProtocol,
        confirmation_handler: ConfirmationHandlerProtocol | None = None,
        delegation_executor: DelegationExecutorProtocol | None = None,
        mcp_client_manager: Any = None,
        parent_agent_getter: Callable[[], Any | None] | None = None,
        max_concurrent: int = 5,
    ) -> None:
        self._registry = registry
        self._confirmation = confirmation_handler
        self._delegation = delegation_executor
        self._mcp = mcp_client_manager
        self._parent_agent_getter = parent_agent_getter
        self._max_concurrent = max_concurrent

    async def execute(
        self, tool_call_request: ToolCallRequest
    ) -> tuple[ToolResult, ToolExecutionMeta]:
        start = time.monotonic()
        tool_name = tool_call_request.function_name

        # Lookup
        if not self._registry.has_tool(tool_name):
            return self._not_found(tool_call_request, start)

        entry = self._registry.get_tool(tool_name)

        # Confirmation
        if entry.meta.require_confirm and self._confirmation:
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

        # Execute
        try:
            output = await self._route_execution(entry, validated)
            return (
                ToolResult(
                    tool_call_id=tool_call_request.id,
                    tool_name=tool_name,
                    success=True,
                    output=output,
                ),
                self._meta(entry, start),
            )
        except Exception as e:
            return self._handle_tool_error(tool_name, e, entry, start)

    async def batch_execute(
        self, tool_call_requests: list[ToolCallRequest]
    ) -> list[tuple[ToolResult, ToolExecutionMeta]]:
        sem = asyncio.Semaphore(self._max_concurrent)

        async def _run(req: ToolCallRequest) -> tuple[ToolResult, ToolExecutionMeta]:
            async with sem:
                return await self.execute(req)

        return await asyncio.gather(*[_run(r) for r in tool_call_requests])

    def is_tool_allowed(self, tool_name: str, policy: CapabilityPolicy) -> bool:
        """Hard runtime gate for tool permission ceiling."""
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
        """Route to appropriate executor based on tool source."""
        source = tool_entry.meta.source
        tool_name = tool_entry.meta.name

        logger.info(
            "tool.routing",
            tool_name=tool_name,
            source=source,
            arguments_keys=list(validated_arguments.keys()),
        )

        if source == "local":
            if tool_entry.callable_ref is None:
                raise RuntimeError(f"No callable for tool {tool_name}")
            if tool_entry.meta.is_async:
                return await tool_entry.callable_ref(**validated_arguments)
            else:
                return await asyncio.to_thread(tool_entry.callable_ref, **validated_arguments)

        if source == "mcp":
            if self._mcp is None:
                raise RuntimeError("MCPClientManager not configured")
            server_id = tool_entry.meta.mcp_server_id
            logger.info("tool.routing.mcp", tool_name=tool_name, server_id=server_id)
            return await self._mcp.call_mcp_tool(
                server_id, tool_entry.meta.name, validated_arguments
            )

        if source == "a2a":
            if self._delegation is None:
                raise RuntimeError("DelegationExecutor not configured")
            agent_url = tool_entry.meta.a2a_agent_url or ""
            logger.info("tool.routing.a2a", tool_name=tool_name, agent_url=agent_url)
            result = await self._delegation.delegate_to_a2a(
                agent_url=agent_url,
                task_input=str(validated_arguments.get("task_input", "")),
                skill_id=validated_arguments.get("skill_id"),
            )
            return result.final_answer if result.success else result.error

        if source == "subagent":
            if self._delegation is None:
                raise RuntimeError("DelegationExecutor not configured")
            from agent_framework.models.subagent import MemoryScope, SpawnMode, SubAgentSpec

            # Map all spawn_agent params (doc 14.6)
            mode_str = validated_arguments.get("mode", "ephemeral").upper()
            scope_str = validated_arguments.get("memory_scope", "isolated").upper()
            parent_agent = self._parent_agent_getter() if self._parent_agent_getter else None
            # Propagate parent_run_id for quota tracking
            parent_run_id = ""
            if parent_agent and hasattr(parent_agent, "agent_id"):
                parent_run_id = parent_agent.agent_id

            spec = SubAgentSpec(
                parent_run_id=parent_run_id,
                task_input=validated_arguments.get("task_input", ""),
                mode=SpawnMode(mode_str) if mode_str in SpawnMode.__members__ else SpawnMode.EPHEMERAL,
                skill_id=validated_arguments.get("skill_id"),
                tool_category_whitelist=validated_arguments.get("tool_categories"),
                memory_scope=MemoryScope(scope_str) if scope_str in MemoryScope.__members__ else MemoryScope.ISOLATED,
            )
            parent_id = getattr(parent_agent, "agent_id", "unknown") if parent_agent else "none"

            logger.info(
                "tool.routing.subagent",
                task_input=spec.task_input[:150],
                mode=mode_str,
                memory_scope=scope_str,
                parent_agent_id=parent_id,
                allow_spawn=getattr(getattr(parent_agent, "agent_config", None), "allow_spawn_children", "N/A"),
            )

            result = await self._delegation.delegate_to_subagent(spec, parent_agent)

            logger.info(
                "tool.routing.subagent.done",
                spawn_id=result.spawn_id,
                success=result.success,
                iterations_used=result.iterations_used,
                answer_preview=(result.final_answer or result.error or "")[:120],
            )

            from agent_framework.tools.delegation import DelegationExecutor
            summary = DelegationExecutor.summarize_result(result)
            return summary.model_dump()

        raise RuntimeError(f"Unknown tool source: {source}")

    def _handle_tool_error(
        self, tool_name: str, error: Exception, entry: ToolEntry | None = None, start: float = 0.0
    ) -> tuple[ToolResult, ToolExecutionMeta]:
        logger.error(
            "tool.failed",
            tool_name=tool_name,
            error=str(error),
        )
        source = entry.meta.source if entry else "local"
        return (
            ToolResult(
                tool_call_id="",
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

    def _meta(self, entry: ToolEntry, start: float) -> ToolExecutionMeta:
        return ToolExecutionMeta(
            execution_time_ms=int((time.monotonic() - start) * 1000),
            source=entry.meta.source,
        )

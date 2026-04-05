"""A2A (Agent-to-Agent) protocol client adapter.

Handles discovery, delegation, streaming, and server exposure for
remote A2A agents. All message parsing consolidated in _extract_text_from_parts().

Fixes applied:
- TaskState: handles completed/failed/canceled/running (not just completed)
- skill_id: wired into message metadata when constructing task
- Message parsing: single _extract_text_from_parts() replaces scattered getattr
- Streaming delegation: exposed via delegate_task_streaming()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, AsyncIterator

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import (
    CheckpointLevel,
    DelegationCapabilities,
    SubAgentResult,
    SubAgentStatus,
)
from agent_framework.models.tool import ToolEntry, ToolMeta

if TYPE_CHECKING:
    from agent_framework.protocols.a2a.a2a_discovery_cache import (
        SQLiteA2ADiscoveryCache,
    )

logger = get_logger(__name__)


# ── Message text extraction (single source of truth) ──────────────

def _extract_text_from_parts(parts: Any) -> str:
    """Extract text content from A2A message parts.

    Handles both SDK structures:
    - part.root.text (pydantic discriminated union)
    - part.text (direct attribute)
    - part["text"] (dict fallback)

    Returns concatenated text from all parts.
    """
    if not parts:
        return ""
    texts: list[str] = []
    for part in parts:
        # Pydantic discriminated union: part.root.text
        root = getattr(part, "root", None)
        if root is not None and hasattr(root, "text"):
            texts.append(str(root.text))
            continue
        # Direct attribute: part.text
        if hasattr(part, "text"):
            texts.append(str(part.text))
            continue
        # Dict fallback
        if isinstance(part, dict) and "text" in part:
            texts.append(str(part["text"]))
    return "\n".join(texts)


def _extract_message_text(message: Any) -> str:
    """Extract text from an A2A Message object."""
    if message is None:
        return ""
    parts = getattr(message, "parts", None)
    return _extract_text_from_parts(parts)


class A2AClientAdapter:
    """Client adapter for Agent-to-Agent (A2A) protocol.

    Responsibilities:
    - Discover remote agents via A2A agent cards
    - Delegate tasks to remote A2A agents (sync + streaming)
    - Convert A2A responses to framework SubAgentResult
    - Register discovered agent capabilities as tools
    - Expose local framework as A2A server
    """

    def __init__(
        self,
        discovery_cache: SQLiteA2ADiscoveryCache | None = None,
        discovery_cache_ttl_seconds: int = 3600,
    ) -> None:
        self._known_agents: dict[str, dict] = {}  # alias -> agent card info
        self._clients: dict[str, Any] = {}  # alias -> A2AClient
        self._capabilities: dict[str, DelegationCapabilities] = {}  # alias -> caps
        self._discovery_cache = discovery_cache
        self._discovery_cache_ttl_seconds = discovery_cache_ttl_seconds

    # ── Discovery ──────────────────────────────────────────────────

    async def discover_agent(
        self, agent_url: str, alias: str | None = None
    ) -> dict:
        """Discover a remote agent via its A2A agent card.

        When a discovery cache is configured, cached (non-expired) results
        are returned immediately without making a network RPC.
        """
        # ── Cache lookup ──────────────────────────────────────────
        if self._discovery_cache is not None:
            cached = self._discovery_cache.get(agent_url)
            if cached is not None:
                effective_alias = alias or cached.get("name") or agent_url.split("/")[-1]
                self._known_agents[effective_alias] = cached
                # Restore lightweight capabilities from cached card
                self._capabilities.setdefault(effective_alias, DelegationCapabilities())
                logger.info(
                    "a2a.agent_discovered_from_cache",
                    alias=effective_alias,
                    url=agent_url,
                )
                return cached

        # ── Live discovery ────────────────────────────────────────
        try:
            from a2a.client import A2AClient
        except ImportError:
            raise ImportError(
                "A2A SDK not installed. Install with: pip install 'a2a-python>=0.2.8'"
            )

        try:
            client = await A2AClient.get_client_from_agent_card_url(
                agent_url.rstrip("/") + "/.well-known/agent.json"
            )
            agent_card = client.agent_card
            effective_alias = alias or getattr(agent_card, "name", None) or agent_url.split("/")[-1]

            self._clients[effective_alias] = client
            agent_info: dict = {
                "url": agent_url,
                "name": getattr(agent_card, "name", effective_alias),
                "description": getattr(agent_card, "description", ""),
                "skills": [
                    {
                        "id": getattr(s, "id", ""),
                        "name": getattr(s, "name", ""),
                        "description": getattr(s, "description", ""),
                    }
                    for s in getattr(agent_card, "skills", [])
                ],
            }
            self._known_agents[effective_alias] = agent_info

            # Extract DelegationCapabilities from agent card (boundary §10)
            remote_caps = getattr(agent_card, "capabilities", None)
            caps = DelegationCapabilities(
                supports_progress_events=getattr(remote_caps, "streaming", False) if remote_caps else False,
                supports_typed_questions=getattr(remote_caps, "pushNotifications", False) if remote_caps else False,
                supports_suspend_resume=False,  # A2A doesn't natively support this
                supports_checkpointing=False,
                supports_artifact_streaming=getattr(remote_caps, "streaming", False) if remote_caps else False,
                checkpoint_level=CheckpointLevel.NONE,
            )
            self._capabilities[effective_alias] = caps

            # ── Persist to cache ──────────────────────────────────
            if self._discovery_cache is not None:
                self._discovery_cache.put(
                    agent_url, agent_info, self._discovery_cache_ttl_seconds
                )

            logger.info(
                "a2a.agent_discovered",
                alias=effective_alias,
                url=agent_url,
                skills_count=len(agent_info["skills"]),
                supports_streaming=caps.supports_progress_events,
            )
            return agent_info

        except Exception as e:
            logger.error("a2a.discover_failed", url=agent_url, error=str(e))
            raise

    # ── Delegation (sync) ──────────────────────────────────────────

    async def delegate_task(
        self,
        alias: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> SubAgentResult:
        """Send a task to a remote A2A agent and wait for completion.

        Handles all TaskState values: completed, failed, canceled, running.
        """
        client = self._clients.get(alias)
        if client is None:
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"A2A agent '{alias}' not discovered. Call discover_agent() first.",
            )

        try:
            import uuid
            from a2a.types import TaskState

            task_id = str(uuid.uuid4())
            message = self._build_message(task_input, skill_id)

            final_answer: str | None = None
            final_state: str = "unknown"
            error_msg: str | None = None

            async for event in client.send_message(message):
                if not isinstance(event, tuple):
                    continue
                task, _update = event
                state = task.status.state
                final_state = str(state)

                if state == TaskState.completed:
                    if task.status.message:
                        final_answer = _extract_message_text(task.status.message)
                elif state == TaskState.failed:
                    error_msg = _extract_message_text(
                        getattr(task.status, "message", None)
                    ) or "Task failed on remote agent"
                elif state == TaskState.canceled:
                    error_msg = "Task was canceled on remote agent"
                # TaskState.running / other states → continue waiting

            success = final_answer is not None and error_msg is None

            # Map A2A TaskState to unified SubAgentStatus
            final_status = SubAgentStatus.COMPLETED
            if not success:
                if "canceled" in final_state.lower():
                    final_status = SubAgentStatus.CANCELLED
                elif "failed" in final_state.lower():
                    final_status = SubAgentStatus.FAILED
                elif "input_required" in final_state.lower():
                    final_status = SubAgentStatus.WAITING_USER
                else:
                    final_status = SubAgentStatus.FAILED

            logger.info(
                "a2a.task_completed",
                alias=alias,
                success=success,
                final_state=final_state,
                unified_status=final_status.value,
            )

            return SubAgentResult(
                spawn_id=task_id,
                success=success,
                final_status=final_status,
                final_answer=final_answer,
                error=error_msg or (None if success else "No answer received from A2A agent"),
            )

        except Exception as e:
            logger.error("a2a.task_failed", alias=alias, error=str(e))
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"A2A delegation failed: {e}",
            )

    async def delegate_task_to_agent(
        self,
        agent_url: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> SubAgentResult:
        """Compatibility method: delegate by agent URL."""
        alias = self.resolve_alias(agent_url)
        if alias is None:
            await self.discover_agent(agent_url)
            alias = self.resolve_alias(agent_url)
        if alias is None:
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"A2A agent at {agent_url} not discoverable",
            )
        return await self.delegate_task(alias, task_input, skill_id)

    # ── Delegation (streaming) ─────────────────────────────────────

    async def delegate_task_streaming(
        self, alias: str, task_input: str, skill_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Delegate a task with streaming response (by alias)."""
        client = self._get_client(alias)
        try:
            from a2a.types import TaskState

            message = self._build_message(task_input, skill_id)
            logger.info("a2a.streaming_started", alias=alias)

            async for event in client.send_message_streaming(message):
                if isinstance(event, tuple):
                    task, _update = event
                    state = task.status.state
                    result: dict = {
                        "type": "update",
                        "task_id": task.id,
                        "state": str(state),
                    }
                    if task.status.message:
                        result["message"] = _extract_message_text(task.status.message)
                    if state in (TaskState.failed, TaskState.canceled):
                        result["error"] = result.get("message", str(state))
                    yield result
                else:
                    yield {"type": "event", "data": str(event)}
        except Exception as e:
            logger.error("a2a.streaming_failed", alias=alias, error=str(e))
            yield {"type": "error", "data": str(e)}

    async def stream_task_to_agent(
        self,
        agent_url: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> AsyncIterator[dict]:
        """Stream a task to a remote A2A agent by URL."""
        alias = self.resolve_alias(agent_url)
        if alias is None:
            try:
                await self.discover_agent(agent_url)
            except Exception:
                pass
            alias = self.resolve_alias(agent_url)
        if alias is None:
            yield {"type": "error", "data": f"A2A agent '{agent_url}' not discovered"}
            return
        async for event in self.delegate_task_streaming(alias, task_input, skill_id):
            yield event

    # ── Task management ────────────────────────────────────────────

    async def get_task(self, alias: str, task_id: str) -> dict:
        """Retrieve task status from a remote A2A agent."""
        client = self._get_client(alias)
        try:
            from a2a.types import TaskQueryParams
            task = await client.get_task(TaskQueryParams(id=task_id))
            result: dict = {
                "task_id": task.id,
                "state": str(task.status.state),
                "message": None,
                "artifacts": [],
            }
            if task.status.message:
                result["message"] = _extract_message_text(task.status.message)
            if hasattr(task, "artifacts") and task.artifacts:
                for artifact in task.artifacts:
                    art_parts = _extract_text_from_parts(getattr(artifact, "parts", []))
                    result["artifacts"].append({
                        "id": getattr(artifact, "artifact_id", ""),
                        "name": getattr(artifact, "name", ""),
                        "text": art_parts,
                    })
            logger.info("a2a.task_retrieved", alias=alias, task_id=task_id)
            return result
        except Exception as e:
            logger.error("a2a.get_task_failed", alias=alias, task_id=task_id, error=str(e))
            raise

    async def cancel_task(self, alias: str, task_id: str) -> dict:
        """Cancel a running task on a remote A2A agent."""
        client = self._get_client(alias)
        try:
            from a2a.types import TaskIdParams
            task = await client.cancel_task(TaskIdParams(id=task_id))
            logger.info("a2a.task_cancelled", alias=alias, task_id=task_id)
            return {"task_id": task_id, "state": str(task.status.state)}
        except Exception as e:
            logger.error("a2a.cancel_task_failed", alias=alias, task_id=task_id, error=str(e))
            raise

    async def resubscribe(self, alias: str, task_id: str) -> AsyncIterator[dict]:
        """Resubscribe to streaming updates for an existing task."""
        client = self._get_client(alias)
        try:
            from a2a.types import TaskIdParams
            logger.info("a2a.task_resubscribed", alias=alias, task_id=task_id)
            async for event in client.resubscribe(TaskIdParams(id=task_id)):
                if isinstance(event, tuple):
                    task, _update = event
                    yield {
                        "type": "update",
                        "task_id": task.id,
                        "state": str(task.status.state),
                        "message": _extract_message_text(
                            getattr(task.status, "message", None)
                        ),
                    }
                else:
                    yield {"type": "event", "data": str(event)}
        except Exception as e:
            logger.error("a2a.resubscribe_failed", alias=alias, task_id=task_id, error=str(e))
            yield {"type": "error", "data": str(e)}

    # ── Tool catalog sync ──────────────────────────────────────────

    def sync_agents_to_catalog(self, catalog: Any) -> int:
        """Register discovered A2A agent skills as tools in the catalog."""
        count = 0
        for alias, info in self._known_agents.items():
            meta = ToolMeta(
                name=f"delegate_to_{alias}",
                description=f"Delegate a task to remote agent: {info.get('description', alias)}",
                parameters_schema={
                    "type": "object",
                    "properties": {
                        "task_input": {
                            "type": "string",
                            "description": "The task to send to the remote agent",
                        },
                        "skill_id": {
                            "type": "string",
                            "description": "Optional skill ID to target on the remote agent",
                        },
                    },
                    "required": ["task_input"],
                },
                source="a2a",
                a2a_agent_url=info.get("url", ""),
                is_async=True,
            )
            entry = ToolEntry(meta=meta, callable_ref=None, validator_model=None)
            catalog.register(entry)
            count += 1
        logger.info("a2a.tools_synced", total=count)
        return count

    def list_known_agents(self) -> list[dict]:
        return list(self._known_agents.values())

    # ── Server exposure ────────────────────────────────────────────

    def build_a2a_server_app(
        self,
        framework: Any,
        *,
        name: str = "aegis-agent",
        description: str = "Aegis Agent Framework A2A Server",
        url: str = "http://localhost:8080",
        skills: list[dict] | None = None,
    ) -> Any:
        """Build a FastAPI app that exposes the local framework as an A2A server."""
        try:
            from a2a.server.agent_execution import AgentExecutor, RequestContext
            from a2a.server.events import EventQueue
            from a2a.server.request_handlers import DefaultRequestHandler
            from a2a.server.tasks import InMemoryTaskStore
            from a2a.server.apps import A2AFastAPIApplication
            from a2a.types import (
                AgentCard, AgentCapabilities, AgentSkill,
                Task, TaskStatus, TaskState,
            )
            from a2a.utils.message import new_agent_text_message
        except ImportError:
            raise ImportError(
                "A2A server requires a2a-python. Install with: pip install 'a2a-python>=0.2.8'"
            )

        fw_ref = framework

        class _FrameworkExecutor(AgentExecutor):
            async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
                user_text = _extract_message_text(context.message)

                try:
                    result = await fw_ref.run(user_text)
                    answer = result.final_answer or ""
                    state = TaskState.completed
                except Exception as e:
                    answer = f"Error: {e}"
                    state = TaskState.failed

                await event_queue.enqueue_event(
                    Task(
                        id=context.task_id,
                        context_id=context.context_id,
                        status=TaskStatus(
                            state=state,
                            message=new_agent_text_message(
                                answer, context.context_id, context.task_id,
                            ),
                        ),
                    )
                )

            async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
                await event_queue.enqueue_event(
                    Task(
                        id=context.task_id,
                        context_id=context.context_id,
                        status=TaskStatus(state=TaskState.canceled),
                    )
                )

        agent_skills = [
            AgentSkill(
                id=s.get("id", "default"),
                name=s.get("name", "Default"),
                description=s.get("description", ""),
                tags=s.get("tags", ["agent"]),
            )
            for s in (skills or [{"id": "chat", "name": "Chat", "description": description, "tags": ["agent"]}])
        ]

        agent_card = AgentCard(
            name=name,
            description=description,
            url=url,
            version="1.0.0",
            capabilities=AgentCapabilities(streaming=False),
            skills=agent_skills,
            defaultInputModes=["text/plain"],
            defaultOutputModes=["text/plain"],
        )

        executor = _FrameworkExecutor()
        handler = DefaultRequestHandler(
            agent_executor=executor,
            task_store=InMemoryTaskStore(),
        )
        app_builder = A2AFastAPIApplication(agent_card, handler)
        logger.info("a2a.server_app_built", name=name, url=url)
        return app_builder.build()

    # ── Message construction ───────────────────────────────────────

    @staticmethod
    def _build_message(task_input: str, skill_id: str | None = None) -> Any:
        """Build an A2A message, optionally targeting a specific skill."""
        from a2a.types import (
            Message, MessageSendParams, Part, TextPart, Role,
        )

        parts = [Part(root=TextPart(text=task_input))]
        metadata: dict[str, Any] = {}
        if skill_id:
            metadata["skill_id"] = skill_id

        return MessageSendParams(
            message=Message(
                role=Role.user,
                parts=parts,
                metadata=metadata if metadata else None,
            )
        )

    # ── Capability query (boundary §10) ─────────────────────────────

    def get_capabilities(self, alias: str) -> DelegationCapabilities:
        """Return the declared capabilities for a discovered agent.

        Consumers MUST NOT assume capabilities beyond what is declared.
        """
        return self._capabilities.get(alias, DelegationCapabilities())

    # ── Resume (boundary §7) ─────────────────────────────────────────

    async def resume_task(
        self, remote_task_id: str, resume_payload: dict,
    ) -> SubAgentResult:
        """Resume a waiting remote A2A task by sending additional input.

        Maps to A2A send_message with an existing context_id.
        Returns error if the remote doesn't support resume semantics.
        """
        # Find the client that handles this task
        # For now, try all known clients
        for alias, client in self._clients.items():
            try:
                message_text = resume_payload.get(
                    "answer", resume_payload.get("input", str(resume_payload))
                )
                message = self._build_message(message_text)
                final_answer: str | None = None
                error_msg: str | None = None

                async for event in client.send_message(message):
                    if not isinstance(event, tuple):
                        continue
                    task, _update = event
                    state = task.status.state
                    from a2a.types import TaskState
                    if state == TaskState.completed:
                        if task.status.message:
                            final_answer = _extract_message_text(task.status.message)
                    elif state == TaskState.failed:
                        error_msg = _extract_message_text(
                            getattr(task.status, "message", None)
                        ) or "Resume failed on remote agent"

                success = final_answer is not None and error_msg is None
                return SubAgentResult(
                    spawn_id=remote_task_id,
                    success=success,
                    final_status=SubAgentStatus.COMPLETED if success else SubAgentStatus.FAILED,
                    final_answer=final_answer,
                    error=error_msg,
                )
            except Exception as e:
                continue

        return SubAgentResult(
            spawn_id=remote_task_id,
            success=False,
            error=f"No A2A client found to resume task {remote_task_id}",
        )

    # ── Internal helpers ───────────────────────────────────────────

    def _get_client(self, alias: str) -> Any:
        client = self._clients.get(alias)
        if client is None:
            raise RuntimeError(f"A2A agent '{alias}' not discovered")
        return client

    def resolve_alias(self, agent_url: str) -> str | None:
        """Resolve an agent URL to its alias."""
        for alias, info in self._known_agents.items():
            if info.get("url") == agent_url:
                return alias
        return None

    # Backward compat
    _resolve_alias = resolve_alias

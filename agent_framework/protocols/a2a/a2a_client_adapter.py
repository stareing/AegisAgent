from __future__ import annotations

from typing import Any, AsyncIterator

from agent_framework.infra.logger import get_logger
from agent_framework.models.subagent import SubAgentResult
from agent_framework.models.tool import ToolEntry, ToolMeta

logger = get_logger(__name__)


class A2AClientAdapter:
    """Client adapter for Agent-to-Agent (A2A) protocol.

    Responsibilities:
    - Discover remote agents via A2A agent cards
    - Delegate tasks to remote A2A agents
    - Convert A2A responses to framework SubAgentResult
    - Register discovered agent capabilities as tools
    """

    def __init__(self) -> None:
        self._known_agents: dict[str, dict] = {}  # alias -> agent card info
        self._clients: dict[str, Any] = {}  # alias -> A2AClient

    async def discover_agent(
        self, agent_url: str, alias: str | None = None
    ) -> dict:
        """Discover a remote agent via its A2A agent card.

        Returns the agent card as a dict.
        """
        try:
            from a2a.client import ClientFactory
        except ImportError:
            raise ImportError(
                "A2A SDK not installed. Install with: pip install a2a-sdk"
            )

        try:
            client = await ClientFactory.connect(agent_url.rstrip("/"))
            agent_card = await client.get_card()
            effective_alias = alias or getattr(agent_card, "name", None) or agent_url.split("/")[-1]

            self._clients[effective_alias] = client
            self._known_agents[effective_alias] = {
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

            logger.info(
                "a2a.agent_discovered",
                alias=effective_alias,
                url=agent_url,
                skills_count=len(self._known_agents[effective_alias]["skills"]),
            )
            return self._known_agents[effective_alias]

        except Exception as e:
            logger.error("a2a.discover_failed", url=agent_url, error=str(e))
            raise

    async def delegate_task(
        self,
        alias: str,
        task_input: str,
        skill_id: str | None = None,
    ) -> SubAgentResult:
        """Send a task to a remote A2A agent and wait for completion."""
        client = self._clients.get(alias)
        if client is None:
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"A2A agent '{alias}' not discovered. Call discover_agent() first.",
            )

        try:
            import uuid
            from a2a.client import create_text_message_object
            from a2a.types import TaskState

            task_id = str(uuid.uuid4())
            message = create_text_message_object(content=task_input)

            final_answer: str | None = None
            async for event in client.send_message(message):
                if isinstance(event, tuple):
                    task, _update = event
                    if task.status.state == TaskState.completed and task.status.message:
                        parts_text = []
                        for part in task.status.message.parts:
                            root = getattr(part, "root", part)
                            if hasattr(root, "text"):
                                parts_text.append(root.text)
                        if parts_text:
                            final_answer = "\n".join(parts_text)

            success = final_answer is not None
            logger.info(
                "a2a.task_completed",
                alias=alias,
                success=success,
            )

            return SubAgentResult(
                spawn_id=task_id,
                success=success,
                final_answer=final_answer,
                error=None if success else "No answer received from A2A agent",
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
        alias = self._resolve_alias(agent_url)
        if alias is None:
            await self.discover_agent(agent_url)
            alias = self._resolve_alias(agent_url)
        if alias is None:
            return SubAgentResult(
                spawn_id="",
                success=False,
                error=f"A2A agent at {agent_url} not discoverable",
            )
        return await self.delegate_task(alias, task_input, skill_id)

    def sync_agents_to_catalog(self, catalog: Any) -> int:
        """Register discovered A2A agent skills as tools in the catalog."""
        count = 0
        for alias, info in self._known_agents.items():
            # Register one tool per agent (delegating the whole task)
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

    async def stream_task_to_agent(
        self,
        agent_url: str,
        task_input: str,
    ) -> AsyncIterator[dict]:
        """Stream a task to a remote A2A agent, yielding events as they arrive.

        Yields dicts with keys: type, data.
        """
        alias = self._resolve_alias(agent_url)
        if alias is None:
            try:
                await self.discover_agent(agent_url)
            except Exception:
                pass
            alias = self._resolve_alias(agent_url)

        client = self._clients.get(alias or "")
        if client is None:
            yield {"type": "error", "data": f"A2A agent '{agent_url}' not discovered"}
            return

        try:
            from a2a.client import create_text_message_object

            message = create_text_message_object(content=task_input)

            async for event in client.send_message_streaming(message):
                yield {"type": "event", "data": str(event)}

        except Exception as e:
            logger.error("a2a.stream_failed", alias=alias, error=str(e))
            yield {"type": "error", "data": str(e)}

    def _get_client(self, alias: str) -> Any:
        client = self._clients.get(alias)
        if client is None:
            raise RuntimeError(f"A2A agent '{alias}' not discovered")
        return client

    async def get_task(self, alias: str, task_id: str) -> dict:
        """Retrieve task status from a remote A2A agent."""
        client = self._get_client(alias)
        try:
            from a2a.types import TaskQueryParams
            task = await client.get_task(TaskQueryParams(id=task_id))
            result = {
                "task_id": task.id,
                "state": str(task.status.state),
                "message": None,
                "artifacts": [],
            }
            if task.status.message:
                parts = []
                for part in task.status.message.parts:
                    root = getattr(part, "root", part)
                    if hasattr(root, "text"):
                        parts.append(root.text)
                result["message"] = "\n".join(parts) if parts else None
            if hasattr(task, "artifacts") and task.artifacts:
                for artifact in task.artifacts:
                    art_parts = []
                    for part in getattr(artifact, "parts", []):
                        root = getattr(part, "root", part)
                        if hasattr(root, "text"):
                            art_parts.append(root.text)
                    result["artifacts"].append({
                        "id": getattr(artifact, "artifact_id", ""),
                        "name": getattr(artifact, "name", ""),
                        "parts": art_parts,
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
                    task, update = event
                    yield {
                        "type": "update",
                        "task_id": task.id,
                        "state": str(task.status.state),
                        "data": str(update),
                    }
                else:
                    yield {"type": "event", "data": str(event)}
        except Exception as e:
            logger.error("a2a.resubscribe_failed", alias=alias, task_id=task_id, error=str(e))
            yield {"type": "error", "data": str(e)}

    async def delegate_task_streaming(self, alias: str, task_input: str) -> AsyncIterator[dict]:
        """Delegate a task with streaming response (by alias)."""
        client = self._get_client(alias)
        try:
            from a2a.client import create_text_message_object
            message = create_text_message_object(content=task_input)
            logger.info("a2a.streaming_started", alias=alias)
            async for event in client.send_message_streaming(message):
                if isinstance(event, tuple):
                    task, update = event
                    result: dict = {
                        "type": "update",
                        "task_id": task.id,
                        "state": str(task.status.state),
                    }
                    if task.status.message:
                        parts = []
                        for part in task.status.message.parts:
                            root = getattr(part, "root", part)
                            if hasattr(root, "text"):
                                parts.append(root.text)
                        result["message"] = "\n".join(parts)
                    yield result
                else:
                    yield {"type": "event", "data": str(event)}
        except Exception as e:
            logger.error("a2a.streaming_failed", alias=alias, error=str(e))
            yield {"type": "error", "data": str(e)}

    def build_a2a_server_app(
        self,
        framework: Any,
        *,
        name: str = "aegis-agent",
        description: str = "Aegis Agent Framework A2A Server",
        url: str = "http://localhost:8080",
        skills: list[dict] | None = None,
    ) -> Any:
        """Build a FastAPI app that exposes the local framework as an A2A server.

        Returns a FastAPI app (call uvicorn.run(app, ...) to start).
        """
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
                "A2A server requires a2a-sdk. Install with: pip install a2a-sdk"
            )

        fw_ref = framework

        class _FrameworkExecutor(AgentExecutor):
            async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
                user_text = ""
                if context.message and context.message.parts:
                    for part in context.message.parts:
                        root = getattr(part, "root", part)
                        if hasattr(root, "text"):
                            user_text += root.text

                try:
                    result = await fw_ref.run(user_text)
                    answer = result.final_answer or ""
                except Exception as e:
                    answer = f"Error: {e}"

                await event_queue.enqueue_event(
                    Task(
                        id=context.task_id,
                        context_id=context.context_id,
                        status=TaskStatus(
                            state=TaskState.completed,
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

    def _extract_answer(self, response: Any) -> str | None:
        """Extract text answer from A2A response object."""
        # Handle Task response
        if hasattr(response, "result"):
            result = response.result
            # Task type
            if hasattr(result, "artifacts") and result.artifacts:
                parts_text = []
                for artifact in result.artifacts:
                    for part in getattr(artifact, "parts", []):
                        root = getattr(part, "root", part)
                        if hasattr(root, "text"):
                            parts_text.append(root.text)
                if parts_text:
                    return "\n".join(parts_text)

            # Message type
            if hasattr(result, "parts"):
                parts_text = []
                for part in result.parts:
                    root = getattr(part, "root", part)
                    if hasattr(root, "text"):
                        parts_text.append(root.text)
                if parts_text:
                    return "\n".join(parts_text)

        # Direct message response
        if hasattr(response, "parts"):
            parts_text = []
            for part in response.parts:
                root = getattr(part, "root", part)
                if hasattr(root, "text"):
                    parts_text.append(root.text)
            if parts_text:
                return "\n".join(parts_text)

        return None

    def resolve_alias(self, agent_url: str) -> str | None:
        """Resolve an agent URL to its alias. Public API for DelegationExecutor."""
        for alias, info in self._known_agents.items():
            if info.get("url") == agent_url:
                return alias
        return None

    # Keep old name for backward compat
    _resolve_alias = resolve_alias

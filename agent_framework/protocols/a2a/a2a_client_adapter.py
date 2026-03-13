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
            from a2a.client import A2AClient
        except ImportError:
            raise ImportError(
                "A2A SDK not installed. Install with: pip install a2a-python"
            )

        try:
            client = await A2AClient.get_client_from_agent_card_url(
                f"{agent_url.rstrip('/')}/.well-known/agent.json"
            )
            agent_card = client.agent_card
            effective_alias = alias or agent_card.name or agent_url.split("/")[-1]

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
            from a2a.types import MessageSendParams, TextPart, Part, Message, Role

            task_id = str(uuid.uuid4())

            params = MessageSendParams(
                message=Message(
                    messageId=str(uuid.uuid4()),
                    role=Role.user,
                    parts=[Part(root=TextPart(text=task_input))],
                ),
            )

            response = await client.send_message(params)

            # Parse response
            final_answer = self._extract_answer(response)
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
        alias: str,
        task_input: str,
    ) -> AsyncIterator[dict]:
        """Stream a task to a remote A2A agent, yielding events as they arrive.

        Yields dicts with keys: type, data.
        """
        client = self._clients.get(alias)
        if client is None:
            yield {"type": "error", "data": f"A2A agent '{alias}' not discovered"}
            return

        try:
            import uuid
            from a2a.types import MessageSendParams, TextPart, Part, Message, Role

            params = MessageSendParams(
                message=Message(
                    messageId=str(uuid.uuid4()),
                    role=Role.user,
                    parts=[Part(root=TextPart(text=task_input))],
                ),
            )

            response = await client.send_message_streaming(params)
            async for event in response:
                yield {"type": "event", "data": str(event)}

        except Exception as e:
            logger.error("a2a.stream_failed", alias=alias, error=str(e))
            yield {"type": "error", "data": str(e)}

    async def register_as_a2a_server(
        self,
        agent: Any,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        """Register the current agent as an A2A server.

        Uses the a2a-python SDK to expose the agent via A2A protocol.
        """
        try:
            from a2a.server import A2AServer, TaskHandler

            class _AgentTaskHandler(TaskHandler):
                def __init__(self, agent_ref: Any) -> None:
                    self._agent = agent_ref

                async def handle(self, request: Any) -> Any:
                    # Extract task input from the request
                    task_text = ""
                    if hasattr(request, "message") and hasattr(request.message, "parts"):
                        for part in request.message.parts:
                            root = getattr(part, "root", part)
                            if hasattr(root, "text"):
                                task_text += root.text
                    # Delegate to agent (requires framework.run integration)
                    return {"status": "received", "task": task_text}

            handler = _AgentTaskHandler(agent)
            server = A2AServer(handler=handler, host=host, port=port)

            logger.info("a2a.server_started", host=host, port=port)
            await server.start()

        except ImportError:
            raise ImportError(
                "A2A server requires a2a-python with server support. "
                "Install with: pip install a2a-python[server]"
            )
        except Exception as e:
            logger.error("a2a.server_start_failed", error=str(e))
            raise

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

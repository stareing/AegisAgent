"""Minimal A2A test server for integration testing.

Run: python tests/a2a_test_server.py
Exposes a simple echo agent at http://localhost:9100
"""

from __future__ import annotations

import asyncio

import uvicorn
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.apps import A2AFastAPIApplication
from a2a.types import (
    AgentCard,
    AgentCapabilities,
    AgentSkill,
    Task,
    TaskStatus,
    TaskState,
)
from a2a.utils.message import new_agent_text_message


class EchoAgentExecutor(AgentExecutor):
    """Simple echo agent that returns the input message."""

    async def execute(self, context: RequestContext, event_queue: EventQueue):
        user_text = ""
        if context.message and context.message.parts:
            for part in context.message.parts:
                root = getattr(part, "root", part)
                if hasattr(root, "text"):
                    user_text += root.text

        reply = f"[echo-agent] received: {user_text}"

        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(
                    state=TaskState.completed,
                    message=new_agent_text_message(
                        reply, context.context_id, context.task_id
                    ),
                ),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue):
        await event_queue.enqueue_event(
            Task(
                id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.canceled),
            )
        )


def create_app():
    agent_card = AgentCard(
        name="echo-agent",
        description="A simple echo agent for testing",
        url="http://localhost:9100",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        skills=[
            AgentSkill(
                id="echo",
                name="Echo",
                description="Echoes back your message",
                tags=["echo", "test"],
            )
        ],
        defaultInputModes=["text/plain"],
        defaultOutputModes=["text/plain"],
    )
    executor = EchoAgentExecutor()
    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )
    return A2AFastAPIApplication(agent_card, handler)


if __name__ == "__main__":
    app_builder = create_app()
    app = app_builder.build()
    uvicorn.run(app, host="0.0.0.0", port=9100)

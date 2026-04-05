"""Example: Agent-to-Agent (A2A) delegation.

Demonstrates how to discover and delegate tasks to remote A2A agents.
"""

import asyncio

from agent_framework.entry import AgentFramework


async def main():
    framework = AgentFramework()
    framework.setup(auto_approve_tools=True)

    try:
        from agent_framework.protocols.a2a.a2a_client_adapter import \
            A2AClientAdapter

        a2a = A2AClientAdapter()

        # Discover a remote agent
        agent_card = await a2a.discover_agent(
            agent_url="http://localhost:9000",
            alias="helper_agent",
        )
        print(f"Discovered agent: {agent_card.get('name')}")
        print(f"Skills: {agent_card.get('skills', [])}")

        # Delegate a task directly
        result = await a2a.delegate_task(
            alias="helper_agent",
            task_input="Summarize the latest news.",
        )
        print(f"Success: {result.success}")
        print(f"Answer: {result.final_answer}")

        # Or sync agent as a tool and let the agent decide when to delegate
        a2a.sync_agents_to_catalog(framework._catalog)
        framework_result = await framework.run(
            "I need you to delegate the summarization task to the helper agent."
        )
        print(f"Framework result: {framework_result.final_answer}")

    except ImportError:
        print("A2A SDK not installed. Install with: pip install a2a-python")

    await framework.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

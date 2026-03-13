"""Example: Simple agent with default configuration.

Demonstrates the minimal setup to run an agent.
"""

import asyncio

from agent_framework.entry import AgentFramework
from agent_framework.tools.decorator import tool


@tool(name="greet", description="Greet a person by name.")
def greet(name: str) -> str:
    """Greet someone."""
    return f"Hello, {name}! Nice to meet you."


@tool(name="add", description="Add two numbers together.")
def add(a: float, b: float) -> float:
    """Add two numbers."""
    return a + b


async def main():
    framework = AgentFramework()
    framework.setup(auto_approve_tools=True)

    # Register custom tools
    framework.register_tool(greet)
    framework.register_tool(add)

    # Run a task
    result = await framework.run("Please greet Alice and then calculate 2 + 3.")
    print(f"Success: {result.success}")
    print(f"Answer: {result.final_answer}")
    print(f"Iterations: {result.iterations_used}")

    await framework.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

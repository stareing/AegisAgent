"""Multi-agent orchestration demo.

Demonstrates:
1. Parent agent spawning child agents via spawn_agent tool
2. Async sub-agent execution with result collection
3. A2A inter-agent communication (requires A2A server running)

Usage:
    # Basic multi-agent (mock mode, no API key needed)
    python examples/multi_agent_demo.py

    # With real model
    python examples/multi_agent_demo.py --config config/deepseek.json

    # With A2A echo server (start server first: python tests/a2a_test_server.py)
    python examples/multi_agent_demo.py --config config/deepseek.json --a2a
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_framework.entry import AgentFramework
from agent_framework.infra.config import load_config
from agent_framework.models.message import ToolCallRequest


async def demo_subagent_sync(fw: AgentFramework) -> None:
    """Demo 1: Synchronous sub-agent spawn via tool executor."""
    print("\n" + "=" * 60)
    print("Demo 1: Synchronous Sub-Agent Spawn")
    print("=" * 60)

    executor = fw._deps.tool_executor
    result, meta = await executor.execute(
        ToolCallRequest(
            id="demo-spawn-1",
            function_name="spawn_agent",
            arguments={
                "task_input": "What is 2 + 2? Answer in one word.",
                "wait": True,
                "max_iterations": 2,
                "deadline_ms": 30000,
            },
        )
    )
    print(f"  Success: {result.success}")
    print(f"  Output:  {str(result.output)[:200]}")
    print(f"  Time:    {meta.execution_time_ms}ms")


async def demo_subagent_async(fw: AgentFramework) -> None:
    """Demo 2: Async sub-agent spawn + result collection."""
    print("\n" + "=" * 60)
    print("Demo 2: Async Sub-Agent (fire-and-collect)")
    print("=" * 60)

    executor = fw._deps.tool_executor

    # Spawn async
    spawn_result, _ = await executor.execute(
        ToolCallRequest(
            id="demo-spawn-async",
            function_name="spawn_agent",
            arguments={
                "task_input": "List 3 programming languages.",
                "wait": False,
                "max_iterations": 2,
                "deadline_ms": 30000,
            },
        )
    )
    print(f"  Spawn result: {spawn_result.output}")

    if isinstance(spawn_result.output, dict):
        spawn_id = spawn_result.output.get("spawn_id", "")
        print(f"  Spawn ID: {spawn_id}")

        # Wait a bit then collect
        await asyncio.sleep(2)
        collect_result, _ = await executor.execute(
            ToolCallRequest(
                id="demo-collect",
                function_name="check_spawn_result",
                arguments={"spawn_id": spawn_id, "wait": True},
            )
        )
        print(f"  Collect: {str(collect_result.output)[:200]}")


async def demo_a2a_delegation(fw: AgentFramework) -> None:
    """Demo 3: A2A cross-agent delegation."""
    print("\n" + "=" * 60)
    print("Demo 3: A2A Delegation (requires echo server on port 9100)")
    print("=" * 60)

    # Connect A2A
    fw.config.a2a.known_agents = [
        {"url": "http://localhost:9100", "alias": "echo"}
    ]
    try:
        await fw.setup_a2a()
    except Exception as e:
        print(f"  Skipped: A2A server not available ({e})")
        return

    # Register A2A tools into registry
    tools = fw._registry.list_tools()
    a2a_tools = [t.meta.name for t in tools if t.meta.source == "a2a"]
    print(f"  A2A tools registered: {a2a_tools}")

    if "delegate_to_echo" in a2a_tools:
        executor = fw._deps.tool_executor
        result, meta = await executor.execute(
            ToolCallRequest(
                id="demo-a2a",
                function_name="delegate_to_echo",
                arguments={"task_input": "Hello from multi-agent demo!"},
            )
        )
        print(f"  Success: {result.success}")
        print(f"  Output:  {result.output}")
        print(f"  Time:    {meta.execution_time_ms}ms")


async def demo_parallel_tools(fw: AgentFramework) -> None:
    """Demo 4: Parallel tool execution."""
    print("\n" + "=" * 60)
    print("Demo 4: Parallel Tool Execution")
    print("=" * 60)

    executor = fw._deps.tool_executor
    requests = [
        ToolCallRequest(id="p1", function_name="think", arguments={"thought": "Planning step 1"}),
        ToolCallRequest(id="p2", function_name="think", arguments={"thought": "Planning step 2"}),
        ToolCallRequest(id="p3", function_name="think", arguments={"thought": "Planning step 3"}),
    ]

    results = await executor.batch_execute(requests)
    for (result, meta) in results:
        print(f"  [{result.tool_call_id}] success={result.success} time={meta.execution_time_ms}ms")

    print(f"  All {len(results)} tools executed in parallel")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-agent demo")
    parser.add_argument("--config", help="Config file path")
    parser.add_argument("--a2a", action="store_true", help="Include A2A demo")
    args = parser.parse_args()

    config = load_config(args.config)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    # Use mock model if no config provided
    if args.config is None:
        from agent_framework.terminal_runtime import InteractiveMockModel
        fw._deps.model_adapter = InteractiveMockModel()

    print("Aegis Multi-Agent Demo")
    print(f"Tools: {len(fw._registry.list_tools())}")

    await demo_parallel_tools(fw)
    await demo_subagent_sync(fw)
    await demo_subagent_async(fw)

    if args.a2a:
        await demo_a2a_delegation(fw)

    await fw.shutdown()
    print("\n" + "=" * 60)
    print("All demos completed.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

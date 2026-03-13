"""CLI entry point: lightweight REPL for the agent framework.

Supports both bare commands (``tools``) and slash commands (``/tools``).
For the full interactive terminal with colored output, use ``main.py``.
"""

from __future__ import annotations

import asyncio
import sys

from agent_framework.entry import AgentFramework
from agent_framework.infra.config import load_config


def _print_result(result) -> None:
    if result.success:
        print(f"\n{'='*60}")
        print(f"Agent Answer:")
        print(f"{'='*60}")
        print(result.final_answer or "(no answer)")
        print(f"{'='*60}")
        print(f"Iterations: {result.iterations_used} | Tokens: {result.usage.total_tokens}")
    else:
        print(f"\n[ERROR] Agent failed: {result.error or result.stop_signal}")
        print(f"Iterations: {result.iterations_used} | Tokens: {result.usage.total_tokens}")


def _normalize_cmd(text: str) -> str:
    """Strip optional leading ``/`` and lowercase."""
    stripped = text.lstrip("/")
    return stripped.lower()


async def _repl(framework: AgentFramework) -> None:
    """Interactive REPL loop."""
    print("Agent Framework REPL (type '/help' or 'help' to see commands)")
    print("-" * 50)

    while True:
        try:
            user_input = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting...")
            break

        if not user_input:
            continue

        cmd = _normalize_cmd(user_input)

        if cmd in ("exit", "quit", "q"):
            break

        if cmd == "help":
            print("Commands (use with or without / prefix):")
            print("  /help       — Show this help")
            print("  /tools      — List registered tools")
            print("  /skills     — List registered skills")
            print("  /memories   — List saved memories")
            print("  /config     — Show config summary")
            print("  /exit       — Exit (also: quit, q, Ctrl+C)")
            print()
            print("  Plain text is sent to the Agent as conversation.")
            continue

        if cmd == "tools":
            tools = framework._registry.list_tools() if framework._registry else []
            for t in tools:
                confirm = " [confirm]" if t.meta.require_confirm else ""
                print(f"  - {t.meta.name} ({t.meta.source}/{t.meta.category or '-'}){confirm}: {t.meta.description[:60]}")
            if not tools:
                print("  (no tools registered)")
            continue

        if cmd == "skills":
            skills = framework.list_skills()
            for s in skills:
                kw = ", ".join(s.trigger_keywords) if s.trigger_keywords else "(none)"
                active = " [ACTIVE]" if framework.get_active_skill() and framework.get_active_skill().skill_id == s.skill_id else ""
                print(f"  - {s.skill_id}: {s.name or s.skill_id}{active}")
                print(f"    Keywords: {kw}")
                if s.description:
                    print(f"    Description: {s.description[:80]}")
            if not skills:
                print("  (no skills registered)")
            continue

        if cmd == "memories":
            mm = framework._deps.memory_manager if framework._deps else None
            if mm:
                records = mm.list_memories(
                    framework._agent.agent_id if framework._agent else "",
                    None,
                )
                for r in records:
                    print(f"  [{r.kind.value}] {r.title}")
                if not records:
                    print("  (no memories)")
            continue

        if cmd == "config":
            cfg = framework.config
            print(f"  Adapter:  {cfg.model.adapter_type}")
            print(f"  Model:    {cfg.model.default_model_name}")
            print(f"  Temp:     {cfg.model.temperature}")
            print(f"  Context:  {cfg.context.max_context_tokens}")
            print(f"  API Base: {cfg.model.api_base or '(default)'}")
            continue

        # Unknown /command
        if user_input.startswith("/"):
            print(f"  Unknown command: {user_input}. Type /help for available commands.")
            continue

        result = await framework.run(user_input)
        _print_result(result)

    await framework.shutdown()


def main() -> None:
    """Main CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="AI Agent Framework CLI")
    parser.add_argument("--config", "-c", help="Path to config JSON file")
    parser.add_argument("--model", "-m", help="Override model name")
    parser.add_argument("--task", "-t", help="Run a single task (non-interactive)")
    parser.add_argument("--auto-approve", action="store_true", help="Auto-approve tool calls")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.model:
        config.model.default_model_name = args.model

    framework = AgentFramework(config=config)
    framework.setup(auto_approve_tools=args.auto_approve)

    if args.task:
        result = asyncio.run(framework.run(args.task))
        _print_result(result)
        asyncio.run(framework.shutdown())
    else:
        asyncio.run(_repl(framework))


if __name__ == "__main__":
    main()

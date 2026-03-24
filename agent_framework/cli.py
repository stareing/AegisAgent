"""Primary CLI entrypoint for the agent framework."""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from collections.abc import Sequence

from agent_framework.terminal_runtime import (build_argument_parser,
                                              build_framework_from_args,
                                              format_missing_textual_message,
                                              run_classic_repl,
                                              run_single_task,
                                              _setup_team)


def run(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser("AI Agent Framework CLI")
    args = parser.parse_args(list(argv) if argv is not None else None)

    # Handle --init before framework setup (no config needed)
    if getattr(args, "init", None):
        from agent_framework.workspace.templates import init_workspace
        template = args.init
        created = init_workspace(template=template)
        if created:
            print(f"工作区已初始化 (模板: {template}):")
            for f in created:
                print(f"  + {f}")
        else:
            print("工作区已存在，无需创建。")
        return 0

    try:
        framework, mock_model = build_framework_from_args(args)
    except Exception as exc:
        print(f"框架初始化失败: {exc}")
        if os.environ.get("DEBUG"):
            traceback.print_exc()
        return 1

    # Auto-start team mode if --team flag
    if getattr(args, "team", False):
        try:
            team_name = framework.config.team.name or "agent_team"
            _setup_team(framework, team_name)
            print(f"  Team 模式已启动: {team_name}")
        except Exception as exc:
            print(f"  Team 启动失败: {exc}")

    if args.task:
        try:
            output = asyncio.run(run_single_task(framework, args.task))
            print(output)
            return 0
        finally:
            asyncio.run(framework.shutdown())

    try:
        from agent_framework.textual_cli import run_textual_cli
    except ModuleNotFoundError as exc:
        if exc.name != "textual":
            raise
        print(format_missing_textual_message(), file=sys.stderr)
        asyncio.run(run_classic_repl(framework, mock_model))
        return 0

    run_textual_cli(framework, mock_model, args.config)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()


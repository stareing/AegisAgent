"""Interactive terminal interface for manual testing of the Agent Framework.

Provides a rich REPL with colored output, command system, built-in mock model,
skill management, tool browsing, memory inspection, and live agent interaction.

Usage:
    python -m agent_framework.main                    # Mock model (no API key needed)
    python -m agent_framework.main --config config/deepseek.json  # Real model
    python -m agent_framework.main --mock              # Force mock mode
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from typing import AsyncIterator, Any

# ---------------------------------------------------------------------------
# ANSI color helpers (no external dependency)
# ---------------------------------------------------------------------------

_NO_COLOR = os.environ.get("NO_COLOR") is not None


def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(text: str) -> str:
    return _c("1", text)


def _dim(text: str) -> str:
    return _c("2", text)


def _green(text: str) -> str:
    return _c("32", text)


def _yellow(text: str) -> str:
    return _c("33", text)


def _cyan(text: str) -> str:
    return _c("36", text)


def _red(text: str) -> str:
    return _c("31", text)


def _magenta(text: str) -> str:
    return _c("35", text)


# ---------------------------------------------------------------------------
# Built-in mock model for offline testing
# ---------------------------------------------------------------------------

from agent_framework.adapters.model.base_adapter import BaseModelAdapter, ModelChunk
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest


class InteractiveMockModel(BaseModelAdapter):
    """Mock model that simulates LLM behavior for interactive testing.

    Recognizes keywords in user input and triggers appropriate tool calls.
    Falls back to echo-style responses for unrecognized input.
    """

    def __init__(self) -> None:
        self._call_count = 0
        self._tool_results: list[str] = []
        self._last_seen_msg_count = 0

    def _reset_turn(self) -> None:
        self._call_count = 0
        self._tool_results = []
        self._last_seen_msg_count = 0

    async def complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        self._call_count += 1

        # Collect tool results from new messages
        new_messages = messages[self._last_seen_msg_count:]
        self._last_seen_msg_count = len(messages)
        for m in new_messages:
            if m.role == "tool" and m.content:
                self._tool_results.append(m.content)

        # If we already have tool results, summarize them
        if self._tool_results and self._call_count > 1:
            summary = "\n".join(f"  - {r}" for r in self._tool_results)
            return ModelResponse(
                content=f"[Mock] 工具执行完成，结果：\n{summary}",
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
            )

        # Extract user input
        user_input = ""
        for m in messages:
            if m.role == "user":
                user_input = m.content or ""

        # Build available tool names
        tool_names = {t["function"]["name"] for t in (tools or [])}

        # Pattern matching for tool dispatch
        input_lower = user_input.lower()
        tool_calls: list[ToolCallRequest] = []

        if "calculator" in tool_names and any(kw in input_lower for kw in ("计算", "算", "calc", "math", "+", "*", "/")):
            # Extract expression or use default
            expr = "42 * 13 + 7"
            for part in user_input.split():
                if any(c in part for c in "0123456789+-*/."):
                    expr = part
                    break
            tool_calls.append(ToolCallRequest(
                id="tc_calc", function_name="calculator",
                arguments={"expression": expr},
            ))

        if "weather" in tool_names and any(kw in input_lower for kw in ("天气", "weather")):
            city = "北京"
            for c in ("北京", "上海", "深圳", "东京", "广州"):
                if c in user_input:
                    city = c
                    break
            tool_calls.append(ToolCallRequest(
                id="tc_weather", function_name="weather",
                arguments={"city": city},
            ))

        if "note" in tool_names and any(kw in input_lower for kw in ("笔记", "记录", "note", "save")):
            tool_calls.append(ToolCallRequest(
                id="tc_note", function_name="note",
                arguments={"title": "用户笔记", "content": user_input},
            ))

        if "read_file" in tool_names and any(kw in input_lower for kw in ("读取文件", "读文件", "read file", "cat ")):
            path = user_input.split()[-1] if len(user_input.split()) > 1 else "."
            tool_calls.append(ToolCallRequest(
                id="tc_read", function_name="read_file",
                arguments={"path": path},
            ))

        if "list_directory" in tool_names and any(kw in input_lower for kw in ("列出文件", "目录", "ls", "list dir")):
            path = user_input.split()[-1] if len(user_input.split()) > 1 else "."
            tool_calls.append(ToolCallRequest(
                id="tc_ls", function_name="list_directory",
                arguments={"path": path},
            ))

        if "run_command" in tool_names and any(kw in input_lower for kw in ("执行命令", "运行命令", "run command", "shell")):
            cmd = user_input.split(maxsplit=1)[-1] if " " in user_input else "echo hello"
            tool_calls.append(ToolCallRequest(
                id="tc_cmd", function_name="run_command",
                arguments={"command": cmd},
            ))

        if tool_calls:
            return ModelResponse(
                content=f"[Mock] 识别到意图，正在调用工具...",
                tool_calls=tool_calls, finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
            )

        # Default echo response
        return ModelResponse(
            content=f"[Mock 模型回复] 收到: {user_input}\n\n(这是 Mock 模型的模拟回复。使用 --config 指定真实模型配置来连接 API。)",
            tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
        )

    async def stream_complete(self, messages, tools=None) -> AsyncIterator[ModelChunk]:
        resp = await self.complete(messages, tools)
        yield ModelChunk(delta_content=resp.content, finish_reason=resp.finish_reason)

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(m.content or "") // 4 for m in messages)

    def supports_parallel_tool_calls(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Built-in demo tools
# ---------------------------------------------------------------------------

from agent_framework.tools.decorator import tool


@tool(name="calculator", description="计算数学表达式", category="math")
def calculator(expression: str) -> str:
    """安全地计算数学表达式。"""
    allowed = set("0123456789+-*/.() ")
    if not all(c in allowed for c in expression):
        return f"错误：表达式包含非法字符: {expression}"
    try:
        result = eval(expression)  # demo only — safe in controlled context
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误: {e}"


@tool(name="weather", description="查询城市天气（模拟数据）", category="info")
def weather(city: str) -> str:
    """查询指定城市的天气信息。"""
    fake_data = {
        "北京": "晴天, 28°C, 湿度 40%",
        "上海": "多云, 25°C, 湿度 65%",
        "深圳": "阵雨, 30°C, 湿度 80%",
        "东京": "晴天, 22°C, 湿度 55%",
        "广州": "多云, 32°C, 湿度 75%",
    }
    return fake_data.get(city, f"未找到 {city} 的天气数据")


@tool(name="note", description="保存一条笔记", category="util")
def note(title: str, content: str) -> str:
    """保存笔记。"""
    return f"已保存笔记 [{title}]: {content}"


# ---------------------------------------------------------------------------
# Built-in demo skills
# ---------------------------------------------------------------------------

from agent_framework.models.agent import Skill

BUILTIN_SKILLS = [
    Skill(
        skill_id="math_expert",
        name="数学专家",
        description="激活数学专家模式，提供详细计算步骤",
        trigger_keywords=["数学", "计算", "math", "calculate"],
        system_prompt_addon="你是一位数学专家。请用清晰的步骤解释计算过程，给出精确结果。",
    ),
    Skill(
        skill_id="translator",
        name="翻译助手",
        description="激活翻译模式，进行中英互译",
        trigger_keywords=["翻译", "translate", "英译中", "中译英"],
        system_prompt_addon="你是一位专业翻译。请准确翻译用户的文本，保持原文风格和语气。",
    ),
    Skill(
        skill_id="code_reviewer",
        name="代码审查",
        description="激活代码审查模式，分析代码质量",
        trigger_keywords=["代码审查", "review", "code review", "审查代码"],
        system_prompt_addon="你是一位资深代码审查专家。请从可读性、性能、安全性、最佳实践等角度分析代码。",
    ),
]


# ---------------------------------------------------------------------------
# Framework builder
# ---------------------------------------------------------------------------

def _build_framework(
    config_path: str | None = None,
    use_mock: bool = False,
    auto_approve: bool = True,
) -> tuple[Any, InteractiveMockModel | None]:
    """Build the framework, optionally with a mock model."""
    import logging
    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    from agent_framework.entry import AgentFramework
    from agent_framework.infra.config import load_config

    config = load_config(config_path)

    mock_model = None
    framework = AgentFramework(config=config)

    if use_mock or (config_path is None and not config.model.api_key):
        # Use mock model — no API key needed
        mock_model = InteractiveMockModel()

        # Manual setup with mock model injected
        framework.setup(auto_approve_tools=auto_approve)
        # Replace the model adapter
        framework._deps.model_adapter = mock_model
    else:
        framework.setup(auto_approve_tools=auto_approve)

    # Register built-in demo tools
    framework.register_tool(calculator)
    framework.register_tool(weather)
    framework.register_tool(note)

    # Register built-in demo skills
    for skill in BUILTIN_SKILLS:
        framework.register_skill(skill)

    return framework, mock_model


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

COMMAND_HELP = {
    "help":     "显示帮助信息",
    "tools":    "列出所有已注册工具",
    "skills":   "列出所有已注册技能",
    "memories": "查看已保存的记忆",
    "config":   "显示当前配置摘要",
    "stats":    "显示上下文统计信息",
    "clear":    "清屏",
    "reset":    "重置 Mock 模型状态",
    "exit":     "退出程序 (也可用 quit / q / Ctrl+C)",
}


def _cmd_help() -> None:
    print(f"\n  {_bold('可用命令:')}")
    for cmd, desc in COMMAND_HELP.items():
        print(f"    {_cyan(cmd):20s} {desc}")
    print(f"\n  {_dim('直接输入文本即可与 Agent 对话。')}")


def _cmd_tools(framework: Any) -> None:
    tools = framework._registry.list_tools() if framework._registry else []
    print(f"\n  {_bold('已注册工具')} ({len(tools)}):")
    for t in tools:
        src = _dim(f"[{t.meta.source}]")
        cat = _dim(f"({t.meta.category})") if t.meta.category else ""
        print(f"    {_green(t.meta.name):30s} {src} {cat}")
        if t.meta.description:
            print(f"      {_dim(t.meta.description[:70])}")
    if not tools:
        print(f"    {_dim('(无工具)')}")


def _cmd_skills(framework: Any) -> None:
    skills = framework.list_skills()
    active = framework.get_active_skill()
    print(f"\n  {_bold('已注册技能')} ({len(skills)}):")
    for s in skills:
        kw = ", ".join(s.trigger_keywords) if s.trigger_keywords else "(none)"
        is_active = _yellow(" [ACTIVE]") if active and active.skill_id == s.skill_id else ""
        print(f"    {_magenta(s.skill_id):20s} {s.name}{is_active}")
        print(f"      关键词: {_dim(kw)}")
        if s.description:
            print(f"      描述: {_dim(s.description[:70])}")
        if s.system_prompt_addon:
            print(f"      Addon: {_dim(s.system_prompt_addon[:60])}...")
    if not skills:
        print(f"    {_dim('(无技能)')}")


def _cmd_memories(framework: Any) -> None:
    mm = framework._deps.memory_manager if framework._deps else None
    if not mm:
        print(f"    {_dim('(记忆系统未初始化)')}")
        return
    agent_id = framework._agent.agent_id if framework._agent else ""
    records = mm.list_memories(agent_id, None)
    print(f"\n  {_bold('已保存记忆')} ({len(records)}):")
    for r in records:
        kind_color = _cyan(f"[{r.kind.value}]")
        pin = _yellow(" [pinned]") if r.pinned else ""
        active = "" if r.active else _dim(" (inactive)")
        print(f"    {kind_color} {r.title}{pin}{active}")
        if r.content:
            print(f"      {_dim(r.content[:80])}")
    if not records:
        print(f"    {_dim('(无记忆)')}")


def _cmd_config(framework: Any) -> None:
    cfg = framework.config
    print(f"\n  {_bold('当前配置:')}")
    print(f"    模型适配器: {_cyan(cfg.model.adapter_type)}")
    print(f"    模型名称:   {_cyan(cfg.model.default_model_name)}")
    print(f"    温度:       {cfg.model.temperature}")
    print(f"    最大输出:   {cfg.model.max_output_tokens}")
    print(f"    API Base:   {cfg.model.api_base or _dim('(default)')}")
    print(f"    上下文窗口: {cfg.context.max_context_tokens}")
    print(f"    压缩策略:   {cfg.context.default_compression_strategy}")
    print(f"    记忆存储:   {cfg.memory.db_path}")
    print(f"    自动提取:   {cfg.memory.auto_extract_memory}")
    skills_count = len(cfg.skills.definitions)
    print(f"    配置技能数: {skills_count}")


def _cmd_stats(framework: Any) -> None:
    try:
        stats = framework._deps.context_engineer.report_context_stats()
        print(f"\n  {_bold('上下文统计:')}")
        print(f"    系统提示 tokens: {stats.system_tokens}")
        print(f"    记忆 tokens:     {stats.memory_tokens}")
        print(f"    会话历史 tokens: {stats.session_tokens}")
        print(f"    当前输入 tokens: {stats.input_tokens}")
        print(f"    总计 tokens:     {_cyan(str(stats.total_tokens))}")
        print(f"    裁剪组数:        {stats.groups_trimmed}")
    except Exception:
        print(f"    {_dim('(尚无统计数据，先发送一条消息)')}")


# ---------------------------------------------------------------------------
# Result printer
# ---------------------------------------------------------------------------

def _print_result(result: Any) -> None:
    if result.success:
        print(f"\n  {_green('Agent 回复:')}")
        print(f"  {'─' * 56}")
        answer = result.final_answer or "(无回答)"
        for line in answer.split("\n"):
            print(f"  {line}")
        print(f"  {'─' * 56}")
        iter_info = f"迭代: {result.iterations_used}"
        token_info = f"Tokens: {result.usage.total_tokens}"
        stop_info = f"停止: {result.stop_signal.reason.value}" if result.stop_signal else ""
        print(f"  {_dim(f'{iter_info} | {token_info} | {stop_info}')}")
    else:
        print(f"\n  {_red('Agent 错误:')}")
        print(f"  {result.error or result.stop_signal}")
        print(f"  {_dim(f'迭代: {result.iterations_used} | Tokens: {result.usage.total_tokens}')}")


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _print_banner(use_mock: bool, config_path: str | None, framework: Any) -> None:
    print()
    print(f"  {_bold(_cyan('╔══════════════════════════════════════════════════════╗'))}")
    print(f"  {_bold(_cyan('║'))}{_bold('       AI Agent Framework — Interactive Terminal       ')}{_bold(_cyan('║'))}")
    print(f"  {_bold(_cyan('╚══════════════════════════════════════════════════════╝'))}")
    print()

    mode = _yellow("Mock 模型 (离线)") if use_mock else _green(f"在线模型: {framework.config.model.default_model_name}")
    print(f"  模式: {mode}")

    if config_path:
        print(f"  配置: {_dim(config_path)}")

    tool_count = len(framework._registry.list_tools()) if framework._registry else 0
    skill_count = len(framework.list_skills())
    print(f"  工具: {_cyan(str(tool_count))} 个  |  技能: {_magenta(str(skill_count))} 个")
    print()
    print(f"  {_dim('输入 help 查看命令列表，直接输入文本与 Agent 对话')}")
    print(f"  {_dim('输入 exit/quit/q 或按 Ctrl+C 退出')}")
    print()


# ---------------------------------------------------------------------------
# Main REPL
# ---------------------------------------------------------------------------

async def _repl(framework: Any, mock_model: InteractiveMockModel | None) -> None:
    """Interactive REPL loop."""
    while True:
        try:
            user_input = input(f"{_bold(_green('> '))}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_dim('再见!')}")
            break

        if not user_input:
            continue

        cmd = user_input.lower()

        if cmd in ("exit", "quit", "q"):
            print(f"{_dim('再见!')}")
            break
        elif cmd == "help":
            _cmd_help()
            continue
        elif cmd == "tools":
            _cmd_tools(framework)
            continue
        elif cmd == "skills":
            _cmd_skills(framework)
            continue
        elif cmd == "memories":
            _cmd_memories(framework)
            continue
        elif cmd == "config":
            _cmd_config(framework)
            continue
        elif cmd == "stats":
            _cmd_stats(framework)
            continue
        elif cmd == "clear":
            os.system("cls" if os.name == "nt" else "clear")
            continue
        elif cmd == "reset":
            if mock_model:
                mock_model._reset_turn()
                print(f"  {_green('Mock 模型状态已重置')}")
            else:
                print(f"  {_dim('(非 Mock 模式，无需重置)')}")
            continue

        # Agent conversation
        if mock_model:
            mock_model._reset_turn()

        try:
            result = await framework.run(user_input)
            _print_result(result)
        except Exception as e:
            print(f"\n  {_red('运行错误:')}")
            print(f"  {e}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()

    await framework.shutdown()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="AI Agent Framework — Interactive Terminal",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m agent_framework.main                           # Mock 模型, 无需 API Key
  python -m agent_framework.main --config config/deepseek.json  # DeepSeek 模型
  python -m agent_framework.main --config config/qwen.json      # 通义千问
  python -m agent_framework.main --mock                    # 强制 Mock 模式
  DEBUG=1 python -m agent_framework.main                   # 显示详细错误
        """,
    )
    parser.add_argument("--config", "-c", help="配置文件路径 (JSON)")
    parser.add_argument("--mock", action="store_true", help="强制使用 Mock 模型")
    parser.add_argument("--no-approve", action="store_true", help="工具调用需要手动确认")
    args = parser.parse_args()

    use_mock = args.mock or (args.config is None)
    auto_approve = not args.no_approve

    try:
        framework, mock_model = _build_framework(
            config_path=args.config,
            use_mock=use_mock,
            auto_approve=auto_approve,
        )
    except Exception as e:
        print(f"{_red('框架初始化失败:')} {e}")
        if os.environ.get("DEBUG"):
            traceback.print_exc()
        sys.exit(1)

    _print_banner(mock_model is not None, args.config, framework)
    asyncio.run(_repl(framework, mock_model))


if __name__ == "__main__":
    main()

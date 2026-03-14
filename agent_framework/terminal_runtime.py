"""Shared terminal runtime for CLI and local debug entrypoints."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import io
import json
import os
import sys
import traceback
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Sequence

from agent_framework.adapters.model.base_adapter import BaseModelAdapter, ModelChunk
from agent_framework.entry import AgentFramework
from agent_framework.infra.config import load_config
from agent_framework.models.agent import Skill
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest
from agent_framework.tools.decorator import tool

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


def _blue(text: str) -> str:
    return _c("34", text)


class InteractiveMockModel(BaseModelAdapter):
    """Keyword-based mock LLM for offline testing."""

    def __init__(self) -> None:
        super().__init__()
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

        new_messages = messages[self._last_seen_msg_count :]
        self._last_seen_msg_count = len(messages)
        for message in new_messages:
            if message.role == "tool" and message.content:
                self._tool_results.append(message.content)

        if self._tool_results and self._call_count > 1:
            summary = "\n".join(f"  - {result}" for result in self._tool_results)
            return ModelResponse(
                content=f"[Mock] 工具执行完成:\n{summary}",
                tool_calls=[],
                finish_reason="stop",
                usage=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
            )

        user_input = ""
        for message in messages:
            if message.role == "user":
                user_input = message.content or ""

        tool_names = {tool_schema["function"]["name"] for tool_schema in (tools or [])}
        lowered_input = user_input.lower()
        calls: list[ToolCallRequest] = []

        if "calculator" in tool_names and any(
            keyword in lowered_input for keyword in ("计算", "算", "calc", "+", "*", "/")
        ):
            expression = "42 * 13 + 7"
            for part in user_input.split():
                if any(char in part for char in "0123456789+-*/."):
                    expression = part
                    break
            calls.append(
                ToolCallRequest(
                    id="tc_calc",
                    function_name="calculator",
                    arguments={"expression": expression},
                )
            )

        if "weather" in tool_names and any(keyword in lowered_input for keyword in ("天气", "weather")):
            city = "北京"
            for known_city in ("北京", "上海", "深圳", "东京", "广州"):
                if known_city in user_input:
                    city = known_city
                    break
            calls.append(
                ToolCallRequest(
                    id="tc_weather",
                    function_name="weather",
                    arguments={"city": city},
                )
            )

        if "note" in tool_names and any(keyword in lowered_input for keyword in ("笔记", "记录", "note")):
            calls.append(
                ToolCallRequest(
                    id="tc_note",
                    function_name="note",
                    arguments={"title": "用户笔记", "content": user_input},
                )
            )

        if "read_file" in tool_names and any(keyword in lowered_input for keyword in ("读文件", "read file", "cat ")):
            path = user_input.split()[-1] if len(user_input.split()) > 1 else "."
            calls.append(
                ToolCallRequest(id="tc_read", function_name="read_file", arguments={"path": path})
            )

        if "list_directory" in tool_names and any(keyword in lowered_input for keyword in ("目录", "ls", "list dir")):
            path = user_input.split()[-1] if len(user_input.split()) > 1 else "."
            calls.append(
                ToolCallRequest(id="tc_ls", function_name="list_directory", arguments={"path": path})
            )

        if "run_command" in tool_names and any(keyword in lowered_input for keyword in ("执行命令", "运行命令", "shell")):
            command = user_input.split(maxsplit=1)[-1] if " " in user_input else "echo hello"
            calls.append(
                ToolCallRequest(
                    id="tc_cmd",
                    function_name="run_command",
                    arguments={"command": command},
                )
            )

        if calls:
            return ModelResponse(
                content="[Mock] 识别意图，调用工具...",
                tool_calls=calls,
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
            )

        return ModelResponse(
            content=f"[Mock] 收到: {user_input}\n(使用 --config 连接真实模型)",
            tool_calls=[],
            finish_reason="stop",
            usage=TokenUsage(prompt_tokens=30, completion_tokens=20, total_tokens=50),
        )

    async def stream_complete(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ModelChunk]:
        response = await self.complete(messages, tools)
        yield ModelChunk(delta_content=response.content, finish_reason=response.finish_reason)

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(message.content or "") // 4 for message in messages)

    def supports_parallel_tool_calls(self) -> bool:
        return True


@tool(name="calculator", description="计算数学表达式", category="math")
def calculator(expression: str) -> str:
    allowed = set("0123456789+-*/.() ")
    if not all(char in allowed for char in expression):
        return f"错误：表达式包含非法字符: {expression}"
    try:
        result = eval(expression)
    except Exception as exc:  # noqa: S307 - demo tool only
        return f"计算错误: {exc}"
    return f"{expression} = {result}"


@tool(name="weather", description="查询城市天气（模拟数据）", category="info")
def weather(city: str) -> str:
    fake_data = {
        "北京": "晴天, 28°C, 湿度 40%",
        "上海": "多云, 25°C, 湿度 65%",
        "深圳": "阵雨, 30°C, 湿度 80%",
        "东京": "晴天, 22°C, 湿度 55%",
        "广州": "多云, 32°C, 湿度 75%",
    }
    return fake_data.get(city, f"未找到 {city} 的天气数据")


def _make_note_tool(memory_manager_ref: list[Any]) -> Any:
    @tool(name="note", description="保存一条笔记到长期记忆", category="util")
    def note(title: str, content: str) -> str:
        manager = memory_manager_ref[0] if memory_manager_ref else None
        if manager is not None:
            from agent_framework.models.memory import (
                MemoryCandidate,
                MemoryCandidateSource,
                MemoryConfidence,
                MemoryKind,
            )

            candidate = MemoryCandidate(
                kind=MemoryKind.CUSTOM,
                title=title,
                content=content,
                tags=["note"],
                reason="User requested note save via tool",
                candidate_source=MemoryCandidateSource.EXPLICIT_USER,
                confidence=MemoryConfidence.HIGH,
            )
            memory_id = manager.remember(candidate)
            if memory_id:
                return f"已保存笔记 [{title}] (memory_id={memory_id})"
        return f"已保存笔记 [{title}]: {content}"

    return note


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


def build_argument_parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", "-c", help="配置文件路径 (JSON)")
    parser.add_argument("--model", "-m", help="覆盖模型名称")
    parser.add_argument("--task", "-t", help="执行单次任务（非交互）")
    parser.add_argument("--mock", action="store_true", help="强制使用 Mock 模型")
    parser.add_argument("--auto-approve", dest="auto_approve", action="store_true", help="自动批准工具调用")
    parser.add_argument("--no-approve", dest="auto_approve", action="store_false", help="工具调用需要手动确认")
    parser.set_defaults(auto_approve=True)
    return parser


def build_framework(
    config_path: str | None = None,
    use_mock: bool = False,
    auto_approve: bool = True,
    model_override: str | None = None,
) -> tuple[AgentFramework, InteractiveMockModel | None]:
    import logging

    logging.getLogger("agent_framework").setLevel(logging.WARNING)
    config = load_config(config_path)
    if model_override:
        config.model.default_model_name = model_override

    framework = AgentFramework(config=config)
    mock_model: InteractiveMockModel | None = None
    if use_mock or (config_path is None and not config.model.api_key):
        mock_model = InteractiveMockModel()
        framework.setup(auto_approve_tools=auto_approve)
        framework._deps.model_adapter = mock_model
    else:
        framework.setup(auto_approve_tools=auto_approve)

    memory_manager_ref: list[Any] = [getattr(framework._deps, "memory_manager", None)] if framework._deps else []
    framework.register_tool(calculator)
    framework.register_tool(weather)
    framework.register_tool(_make_note_tool(memory_manager_ref))
    for skill in BUILTIN_SKILLS:
        framework.register_skill(skill)
    # Start conversation-level session for cross-turn delta optimization
    framework.begin_conversation()
    return framework, mock_model


class ReplState:
    def __init__(self) -> None:
        self.history: list[Message] = []
        self.turn_count = 0
        self.user_id: str | None = None
        self.recent_commands: list[str] = []

    def append_turn(self, user_input: str, result: Any) -> None:
        from agent_framework.agent.message_projector import MessageProjector

        self.history.append(Message(role="user", content=user_input))
        if getattr(result, "iteration_history", None):
            for iteration in result.iteration_history:
                for message in MessageProjector.project_iteration(iteration):
                    self.history.append(
                        Message(
                            role=message.role,
                            content=message.content,
                            tool_calls=message.tool_calls,
                            tool_call_id=message.tool_call_id,
                            name=message.name,
                        )
                    )
        else:
            self.history.append(Message(role="assistant", content=result.final_answer or ""))
        self.turn_count += 1

    def apply_context_editing(self) -> int:
        if len(self.history) < 4:
            return 0

        seen_signatures: set[str] = set()
        keep_indices: set[int] = set()
        stale_tool_call_ids: set[str] = set()

        for index in range(len(self.history) - 1, -1, -1):
            message = self.history[index]
            if message.role == "assistant" and message.tool_calls:
                all_stale = True
                for tool_call in message.tool_calls:
                    signature = f"{tool_call.function_name}|{json.dumps(tool_call.arguments, sort_keys=True, default=str)}"
                    if signature in seen_signatures:
                        stale_tool_call_ids.add(tool_call.id)
                    else:
                        seen_signatures.add(signature)
                        all_stale = False
                if not all_stale:
                    keep_indices.add(index)
            else:
                keep_indices.add(index)

        pruned = 0
        new_history: list[Message] = []
        for index, message in enumerate(self.history):
            if message.role == "tool" and message.tool_call_id in stale_tool_call_ids:
                pruned += 1
                continue
            if index not in keep_indices and message.role == "assistant" and message.tool_calls:
                if all(tool_call.id in stale_tool_call_ids for tool_call in message.tool_calls):
                    pruned += 1
                    continue
            new_history.append(message)

        self.history = new_history
        return pruned

    async def compact(self, model_adapter: Any) -> str:
        """Compress history via layered LLM summarization.

        User-triggered: the most recent user message (current input) is
        excluded from compression and preserved as-is after the summary.
        """
        if len(self.history) < 2:
            return ""

        from agent_framework.context.summarizer import (
            call_llm_compress,
            is_summary_message,
            messages_to_text,
            wrap_summary,
        )

        previous_summary = None
        compress_messages = list(self.history)
        if compress_messages and is_summary_message(compress_messages[0]):
            previous_summary = compress_messages[0].content
            compress_messages = compress_messages[1:]

        if not compress_messages:
            return ""

        # User-triggered: exclude the last user message from compression
        # (it's the "current input" that shouldn't be summarized away)
        preserved_tail: list[Message] = []
        while compress_messages and compress_messages[-1].role == "user":
            preserved_tail.insert(0, compress_messages.pop())

        if not compress_messages:
            return ""

        summary_text = await call_llm_compress(
            messages_to_text(compress_messages),
            model_adapter,
            previous_summary=previous_summary,
            messages=compress_messages,
        )
        if not summary_text:
            return "[Compaction produced empty summary]"

        old_count = len(self.history)
        old_tokens = self._estimate_tokens()
        self.history = [
            Message(role="user", content=wrap_summary(summary_text)),
            *preserved_tail,
        ]
        self.turn_count = 0
        new_tokens = self._estimate_tokens()
        return f"Compacted {old_count} messages (~{old_tokens} tokens) -> 1 summary (~{new_tokens} tokens)"

    def record_command(self, command_name: str) -> None:
        if command_name in self.recent_commands:
            self.recent_commands.remove(command_name)
        self.recent_commands.insert(0, command_name)
        del self.recent_commands[8:]

    def clear(self) -> None:
        self.history.clear()
        self.turn_count = 0

    def message_count(self) -> int:
        return len(self.history)

    def _estimate_tokens(self) -> int:
        total = 0
        for message in self.history:
            text = message.content or ""
            cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
            ascii_chars = len(text) - cjk_chars
            total += ascii_chars // 4 + int(cjk_chars / 1.5)
        return max(total, 1)


_COMMANDS: dict[str, tuple[Any, str, str, str]] = {}


def _register_cmd(name: str, desc: str, usage: str = "", category: str = "通用") -> Any:
    def decorator(func: Any) -> Any:
        _COMMANDS[name] = (func, desc, usage, category)
        return func

    return decorator


@_register_cmd("help", "显示所有可用命令", category="通用")
async def _cmd_help(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    categories: dict[str, list[tuple[str, str, str]]] = {}
    for name, (_, desc, usage, category) in sorted(_COMMANDS.items()):
        categories.setdefault(category, []).append((name, desc, usage))
    print()
    for category, commands in categories.items():
        print(f"  {_bold(_yellow(f'[ {category} ]'))}")
        for name, desc, usage in commands:
            usage_hint = f" {_dim(usage)}" if usage else ""
            print(f"    {_cyan('/' + name):24s} {desc}{usage_hint}")
        print()
    print(f"  {_dim('直接输入文本与 Agent 对话。所有命令均以 / 开头。')}")


@_register_cmd("exit", "退出程序", category="通用")
async def _cmd_exit(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    return


@_register_cmd("reset", "重置对话状态（清空历史 + Mock 状态）", category="通用")
async def _cmd_reset(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    state.clear()
    if mock:
        mock._reset_turn()
    mode_text = "Mock 状态也已重置" if mock else "在线模式"
    print(f"  {_green('对话历史已清空')} ({_dim(mode_text)})")


@_register_cmd("tools", "列出所有已注册工具", category="查看")
async def _cmd_tools(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    tools = fw._registry.list_tools() if fw._registry else []
    tools_by_category: dict[str, list[Any]] = {}
    for tool_entry in tools:
        category = tool_entry.meta.category or "other"
        tools_by_category.setdefault(category, []).append(tool_entry)
    print(f"\n  {_bold('已注册工具')} ({len(tools)}):\n")
    for category, entries in sorted(tools_by_category.items()):
        print(f"  {_yellow(f'  [{category}]')}")
        for entry in entries:
            source = _dim(f"({entry.meta.source})")
            confirm = _red("*") if entry.meta.require_confirm else " "
            print(f"    {confirm} {_green(entry.meta.name):24s} {source}  {_dim(entry.meta.description[:50])}")
        print()
    if not tools:
        print(f"    {_dim('(无工具)')}")
    print(f"  {_dim(_red('*') + ' = 需要确认    使用 /call <tool> 直接调用工具')}")


@_register_cmd("skills", "列出所有已注册技能", category="查看")
async def _cmd_skills(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    skills = fw.list_skills()
    active_skill = fw.get_active_skill()
    print(f"\n  {_bold('已注册技能')} ({len(skills)}):\n")
    for skill in skills:
        keywords = ", ".join(skill.trigger_keywords) if skill.trigger_keywords else "-"
        active = _yellow(" [ACTIVE]") if active_skill and active_skill.skill_id == skill.skill_id else ""
        print(f"    {_magenta(skill.skill_id):20s} {skill.name}{active}")
        print(f"      触发词: {_dim(keywords)}")
        if skill.description:
            print(f"      描述:   {_dim(skill.description[:70])}")
    if not skills:
        print(f"    {_dim('(无技能)')}")
    print(f"\n  {_dim('技能会根据输入关键词自动激活。也可用 /skill <id> 手动激活。')}")


@_register_cmd("memories", "查看已保存的记忆", category="查看")
async def _cmd_memories(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    memory_manager = fw._deps.memory_manager if fw._deps else None
    if not memory_manager:
        print(f"    {_dim('(记忆系统未初始化)')}")
        return
    agent_id = fw._agent.agent_id if fw._agent else ""
    records = memory_manager.list_memories(agent_id, state.user_id)
    print(f"\n  {_bold('已保存记忆')} ({len(records)}):\n")
    for index, record in enumerate(records):
        kind = _cyan(f"[{record.kind.value}]")
        pinned = _yellow(" [pinned]") if record.pinned else ""
        active = "" if record.active else _dim(" (inactive)")
        print(f"    {_dim(f'#{index}')} {kind} {record.title}{pinned}{active}")
        if record.content:
            print(f"       {_dim(record.content[:80])}")
    if not records:
        print(f"    {_dim('(无记忆)')}")


@_register_cmd("config", "显示当前配置摘要", category="查看")
async def _cmd_config(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    config = fw.config
    print(f"\n  {_bold('当前配置:')}")
    rows = [
        ("适配器", config.model.adapter_type),
        ("模型", config.model.default_model_name),
        ("温度", str(config.model.temperature)),
        ("最大输出 tokens", str(config.model.max_output_tokens)),
        ("API Base", config.model.api_base or "(default)"),
        ("上下文窗口", str(config.context.max_context_tokens)),
        ("压缩策略", config.context.default_compression_strategy),
        ("记忆 DB", config.memory.db_path),
        ("自动提取记忆", str(config.memory.auto_extract_memory)),
        ("配置技能数", str(len(config.skills.definitions))),
    ]
    for label, value in rows:
        print(f"    {label:18s} {_cyan(value)}")


@_register_cmd("stats", "显示上下文统计信息", category="查看")
async def _cmd_stats(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    try:
        stats = fw._deps.context_engineer.report_context_stats()
    except Exception:
        print(f"    {_dim('(尚无统计数据，先发送一条消息)')}")
        return
    print(f"\n  {_bold('上下文统计:')}")
    print(f"    系统提示 tokens: {stats.system_tokens}")
    print(f"    记忆 tokens:     {stats.memory_tokens}")
    print(f"    会话历史 tokens: {stats.session_tokens}")
    print(f"    当前输入 tokens: {stats.input_tokens}")
    print(f"    工具 schema tokens: {stats.tools_schema_tokens}")
    print(f"    总计 tokens:     {_cyan(str(stats.total_tokens + stats.tools_schema_tokens))}")
    print(f"    裁剪组数:        {stats.groups_trimmed}")
    print(f"    前缀复用:        {'是' if stats.prefix_reused else '否'}")


@_register_cmd("history", "查看对话历史", usage="/history [n]", category="会话")
async def _cmd_history(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if not state.history:
        print(f"  {_dim('(对话历史为空)')}")
        return
    max_messages = int(args) if args.strip().isdigit() else len(state.history)
    messages = state.history[-max_messages:]
    estimate = state._estimate_tokens()
    print(f"\n  {_bold('对话历史')} ({state.turn_count} 轮, {state.message_count()} 条消息, ~{estimate} tokens):\n")
    for index, message in enumerate(messages):
        if message.role == "user":
            print(f"  {_cyan('User:')} {message.content[:120]}")
        elif message.role == "assistant":
            print(f"  {_green('Agent:')} {(message.content or '')[:120]}")
            if index < len(messages) - 1:
                print()
    print(f"\n  {_dim(f'消息数: {state.message_count()}  |  ~{estimate} tokens')}")


@_register_cmd("history-clear", "清空对话历史", category="会话")
async def _cmd_history_clear(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    state.clear()
    print(f"  {_green('对话历史已清空')}")


@_register_cmd("user", "设置当前 user_id（记忆隔离）", usage="/user <id>|off", category="会话")
async def _cmd_user(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    value = args.strip()
    if not value:
        current = state.user_id if state.user_id else "(未设置)"
        print(f"  当前 user_id: {_cyan(current)}")
        return
    if value.lower() in ("off", "none", "clear"):
        state.user_id = None
        print(f"  {_green('已清除 user_id')}")
        return
    state.user_id = value
    print(f"  {_green('已设置 user_id:')} {_cyan(state.user_id)}")


@_register_cmd("compact", "压缩对话历史为结构化摘要", usage="/compact [custom instruction]", category="会话")
async def _cmd_compact(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if not state.history:
        print(f"  {_dim('对话历史为空，无需压缩')}")
        return

    if mock:
        old_count = len(state.history)
        summary_lines = [f"[{message.role}] {message.content[:100]}" for message in state.history if message.content]
        state.history = [Message(role="user", content=f"<summary>\n" + "\n".join(summary_lines[-6:]) + "\n</summary>")]
        state.turn_count = 0
        print(f"  {_green(f'已压缩 {old_count} 条消息 -> 摘要 (Mock 模式)')}")
        return

    adapter = fw._deps.model_adapter if fw._deps else None
    if not adapter:
        print(f"  {_red('模型适配器未初始化')}")
        return
    print(f"  {_dim('正在压缩对话历史...')}")
    result = await state.compact(adapter)
    if result.startswith("["):
        print(f"  {_red(result)}")
    else:
        print(f"  {_green(result)}")


@_register_cmd("context-edit", "清理重复的工具调用记录", category="会话")
async def _cmd_context_edit(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    pruned = state.apply_context_editing()
    if pruned > 0:
        print(f"  {_green(f'已清理 {pruned} 条重复工具调用记录')}")
    else:
        print(f"  {_dim('无重复工具调用记录')}")


@_register_cmd("call", "直接调用工具", usage="/call <tool_name> {json_args}", category="工具")
async def _cmd_call(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if not args:
        print(f"  {_red('用法:')} /call <tool_name> {{\"arg\": \"value\"}}")
        print(f"  {_dim('示例:')} /call calculator {{\"expression\": \"2+3\"}}")
        return

    parts = args.split(maxsplit=1)
    tool_name = parts[0]
    raw_args = parts[1].strip() if len(parts) > 1 else ""
    registry = fw._registry
    if not registry or not registry.has_tool(tool_name):
        print(f"  {_red('工具不存在:')} {tool_name}")
        print(f"  {_dim('使用 /tools 查看可用工具')}")
        return

    entry = registry.get_tool(tool_name)
    if not entry.callable_ref:
        print(f"  {_red('该工具无本地可调用函数')} (source={entry.meta.source})")
        return

    try:
        if raw_args.startswith("{"):
            call_args = json.loads(raw_args)
        elif raw_args:
            signature = inspect.signature(entry.callable_ref)
            param_names = [name for name in signature.parameters if name != "self"]
            positional = raw_args.split(maxsplit=max(len(param_names) - 1, 0))
            call_args = dict(zip(param_names, positional))
        else:
            call_args = {}
    except json.JSONDecodeError:
        first_param = next(iter(inspect.signature(entry.callable_ref).parameters), None)
        call_args = {first_param: raw_args} if first_param else {}

    print(f"  {_dim(f'调用: {tool_name}({call_args})')}")
    try:
        func = entry.callable_ref
        result = await func(**call_args) if inspect.iscoroutinefunction(func) else func(**call_args)
    except Exception as exc:
        print(f"  {_red('调用失败:')} {exc}")
        return

    print(f"\n  {_green('结果:')}")
    if isinstance(result, (dict, list)):
        print(f"  {json.dumps(result, indent=2, ensure_ascii=False)}")
    else:
        print(f"  {result}")


@_register_cmd("tool", "查看工具详细信息", usage="/tool <name>", category="工具")
async def _cmd_tool(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if not args:
        print(f"  {_red('用法:')} /tool <tool_name>")
        return
    name = args.strip()
    registry = fw._registry
    if not registry or not registry.has_tool(name):
        print(f"  {_red('工具不存在:')} {name}")
        return
    entry = registry.get_tool(name)
    meta = entry.meta
    print(f"\n  {_bold(_green(meta.name))}")
    print(f"    来源:     {meta.source}")
    print(f"    分类:     {meta.category or '-'}")
    print(f"    需确认:   {_red('是') if meta.require_confirm else _green('否')}")
    print(f"    描述:     {meta.description}")
    if meta.tags:
        print(f"    标签:     {', '.join(meta.tags)}")
    if entry.callable_ref:
        signature = inspect.signature(entry.callable_ref)
        print(f"    参数签名: {name}{signature}")
        for param_name, param in signature.parameters.items():
            annotation = param.annotation.__name__ if hasattr(param.annotation, "__name__") else str(param.annotation)
            default = f" = {param.default}" if param.default is not inspect.Parameter.empty else ""
            print(f"      {_cyan(param_name):16s} {_dim(annotation)}{default}")
    if entry.validator_model:
        print(f"    Schema:   {entry.validator_model.model_json_schema()}")


@_register_cmd("skill", "手动激活/查看技能", usage="/skill <id> | /skill off", category="技能")
async def _cmd_skill(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if not args:
        active = fw.get_active_skill()
        if not active:
            print(f"  {_dim('当前无活跃技能。使用 /skill <id> 激活。')}")
            return
        print(f"  当前活跃技能: {_magenta(active.skill_id)} ({active.name})")
        print(f"    Addon: {_dim(active.system_prompt_addon[:80])}")
        return

    if args.strip().lower() == "off":
        fw.deactivate_skill()
        print(f"  {_green('技能已反激活')}")
        return

    skill_id = args.strip()
    router = fw._deps.skill_router
    found = router.get_skill(skill_id)
    if not found:
        print(f"  {_red('技能不存在:')} {skill_id}")
        print(f"  {_dim('可用: ' + ', '.join(skill.skill_id for skill in router.list_skills()))}")
        return
    fw.activate_skill(found)
    print(f"  {_green('已激活技能:')} {_magenta(found.skill_id)} ({found.name})")
    print(f"    Addon: {_dim(found.system_prompt_addon[:80])}")


@_register_cmd("skill-add", "动态注册新技能", usage='/skill-add <id> <keywords> "<addon>"', category="技能")
async def _cmd_skill_add(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if not args:
        print(f"  {_red('用法:')} /skill-add <skill_id> <kw1,kw2,...> \"<system prompt addon>\"")
        return

    parts = args.split(maxsplit=2)
    if len(parts) < 3:
        print(f"  {_red('参数不足。')} 需要: skill_id, keywords, addon_prompt")
        return

    skill_id = parts[0]
    keywords = [keyword.strip() for keyword in parts[1].split(",") if keyword.strip()]
    addon = parts[2].strip().strip('"').strip("'")
    fw.register_skill(
        Skill(
            skill_id=skill_id,
            name=skill_id,
            trigger_keywords=keywords,
            system_prompt_addon=addon,
        )
    )
    print(f"  {_green('已注册技能:')} {_magenta(skill_id)}")
    print(f"    关键词: {', '.join(keywords)}")
    print(f"    Addon:  {_dim(addon[:60])}")


@_register_cmd("skill-rm", "移除一个技能", usage="/skill-rm <id>", category="技能")
async def _cmd_skill_rm(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if not args:
        print(f"  {_red('用法:')} /skill-rm <skill_id>")
        return
    if fw.remove_skill(args.strip()):
        print(f"  {_green('已移除:')} {args.strip()}")
    else:
        print(f"  {_red('未找到:')} {args.strip()}")


@_register_cmd("memory-clear", "清空所有记忆", category="记忆")
async def _cmd_memory_clear(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    print(f"  {_green(f'已清除 {fw.clear_memories()} 条记忆')}")


@_register_cmd("memory-toggle", "开关记忆系统", usage="/memory-toggle on|off", category="记忆")
async def _cmd_memory_toggle(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    value = args.strip().lower()
    if value in ("on", "true", "1"):
        fw.set_memory_enabled(True)
        print(f"  {_green('记忆系统: 已开启')}")
    elif value in ("off", "false", "0"):
        fw.set_memory_enabled(False)
        print(f"  {_yellow('记忆系统: 已关闭')}")
    else:
        print(f"  {_red('用法:')} /memory-toggle on|off")


@_register_cmd("demo", "运行内置演示场景", usage="/demo [calc|weather|multi|skill|note]", category="演示")
async def _cmd_demo(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    demos = {
        "calc": "帮我计算 42*13+7",
        "weather": "查询北京和上海的天气",
        "multi": "算一下 100/3 然后查深圳天气",
        "skill": "帮我用数学方法分析 2**10 的值",
        "note": "帮我记录一条笔记: 今天学习了 Agent Framework",
    }
    if not args or args.strip() not in demos:
        print(f"\n  {_bold('可用演示场景:')}")
        for demo_name, prompt in demos.items():
            print(f"    {_cyan('/demo ' + demo_name):28s} {_dim(prompt)}")
        return
    task = demos[args.strip()]
    print(f"\n  {_dim(f'>>> 模拟输入: {task}')}")
    if mock:
        mock._reset_turn()
    result = await fw.run(task, user_id=state.user_id)
    print(format_result(result))


@_register_cmd("demo-all", "依次运行所有演示场景", category="演示")
async def _cmd_demo_all(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    for demo_name in ["calc", "weather", "multi", "note", "skill"]:
        print(f"\n  {_bold(_yellow(f'--- demo: {demo_name} ---'))}")
        await _cmd_demo(fw, mock, state, demo_name)
        print()


def _render_tool_output(output: Any, tool_name: str) -> str:
    if isinstance(output, dict):
        if tool_name == "run_command" and "stdout" in output:
            parts: list[str] = []
            stdout = output.get("stdout", "").strip()
            stderr = output.get("stderr", "").strip()
            return_code = output.get("return_code", 0)
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"(stderr) {stderr}")
            if not parts:
                parts.append(f"(exit {return_code}, no output)")
            return "\n".join(parts)
        return json.dumps(output, ensure_ascii=False, indent=2)
    if isinstance(output, list):
        return json.dumps(output, ensure_ascii=False, indent=2)
    return str(output) if output else "(no output)"


def _print_result(result: Any) -> None:
    if getattr(result, "iteration_history", None):
        print(f"\n  {_bold('执行轨迹:')}")
        for iteration in result.iteration_history:
            print(f"  {_dim(f'[Iteration {iteration.iteration_index + 1}]')}")
            if getattr(iteration, "llm_input_preview", None):
                print(f"    {_blue('模型输入:')}")
                for line in iteration.llm_input_preview.splitlines():
                    print(f"      {line}")
            response = iteration.model_response
            if response and response.content:
                preview = response.content if len(response.content) <= 500 else response.content[:500] + "\n... [truncated]"
                print(f"    {_cyan('主Agent输出:')}")
                for line in preview.splitlines():
                    print(f"      {line}")
            if response and response.tool_calls:
                print(f"    {_yellow('工具调用:')} {', '.join(call.function_name for call in response.tool_calls)}")
            for tool_result in iteration.tool_results:
                print(f"    {_magenta(f'工具结果[{tool_result.tool_name}]')}:")
                if tool_result.success:
                    rendered = _render_tool_output(tool_result.output, tool_result.tool_name)
                    if len(rendered) > 1000:
                        rendered = rendered[:1000] + "\n... [truncated]"
                    for line in rendered.splitlines():
                        print(f"      {line}")
                else:
                    print(f"      {_red(str(tool_result.error or tool_result.output or '未知错误'))}")

    if result.success:
        print(f"\n  {_green('Agent 回复:')}")
        print(f"  {'─' * 56}")
        for line in (result.final_answer or "(无回答)").splitlines():
            print(f"  {line}")
        print(f"  {'─' * 56}")
        parts = [f"迭代: {result.iterations_used}", f"Tokens: {result.usage.total_tokens}"]
        if result.stop_signal:
            parts.append(f"停止: {result.stop_signal.reason.value}")
        print(f"  {_dim(' | '.join(parts))}")
    else:
        print(f"\n  {_red('Agent 错误:')}")
        print(f"  {result.error or result.stop_signal}")
        print(f"  {_dim(f'迭代: {result.iterations_used} | Tokens: {result.usage.total_tokens}')}")


def format_result(result: Any) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _print_result(result)
    return buffer.getvalue().rstrip()


AEGIS_LOGO_LINES: list[str] = [
    r"    _              _        _                    _   ",
    r"   / \   ___  __ _(_)___   / \   __ _  ___ _ __ | |_ ",
    r"  / _ \ / _ \/ _` | / __| / _ \ / _` |/ _ \ '_ \| __|",
    r" / ___ \  __/ (_| | \__ \/ ___ \ (_| |  __/ | | | |_ ",
    r"/_/   \_\___|\__, |_|___/_/   \_\__, |\___|_| |_|\__|",
    r"             |___/              |___/                 ",
    r"                                      v0.1.0          ",
]

AEGIS_LOGO = "\n".join(AEGIS_LOGO_LINES)


def render_banner(use_mock: bool, config_path: str | None, fw: AgentFramework) -> str:
    logo_lines = [f"  {_bold(_cyan(line))}" for line in AEGIS_LOGO_LINES]
    mode = _yellow("Mock (离线)") if use_mock else _green(f"在线: {fw.config.model.default_model_name}")
    tool_count = len(fw._registry.list_tools()) if fw._registry else 0
    skill_count = len(fw.list_skills())

    lines = ["", *logo_lines, ""]
    if config_path:
        lines.append(f"  {_bold('模式')}  {mode}    {_bold('配置')}  {_dim(config_path)}")
    else:
        lines.append(f"  {_bold('模式')}  {mode}")
    lines.extend([
        f"  {_bold('工具')}  {_cyan(str(tool_count))} 个    {_bold('技能')}  {_magenta(str(skill_count))} 个",
        "",
        f"  {_dim('输入 / 打开命令面板    直接输入文本与 Agent 对话    /exit 退出')}",
        "",
    ])
    return "\n".join(lines)


def _show_slash_menu(filter_text: str = "", fw: AgentFramework | None = None) -> str:
    prefix = filter_text.lower()
    categories: dict[str, list[tuple[str, str]]] = {}
    for name, (_, desc, _, category) in sorted(_COMMANDS.items()):
        if prefix and not name.startswith(prefix):
            continue
        categories.setdefault(category, []).append((name, desc))
    if fw:
        for skill in fw.list_skills():
            display_name = f"skill {skill.skill_id}"
            if prefix and not display_name.startswith(prefix) and not skill.skill_id.startswith(prefix):
                continue
            desc = skill.description or skill.name or skill.skill_id
            categories.setdefault("技能", []).append((display_name, desc[:60]))
    if not categories:
        return f"  {_dim('无匹配命令')}"
    lines = [""]
    all_names = [name for entries in categories.values() for name, _ in entries]
    width = max((len(name) for name in all_names), default=10) + 10
    for category, entries in categories.items():
        lines.append(f"  {_dim('─' * 60)}")
        for name, desc in entries:
            lines.append(f"  {_cyan('/' + name):{width}s} {desc}")
    return "\n".join(lines)


@dataclass(frozen=True)
class CommandPaletteEntry:
    command: str
    title: str
    description: str
    category: str
    usage: str = ""
    keywords: tuple[str, ...] = ()


@dataclass
class CommandExecution:
    output: str = ""
    handled: bool = False
    should_exit: bool = False
    clear_output: bool = False


def build_palette_entries(fw: AgentFramework) -> list[CommandPaletteEntry]:
    entries = [
        CommandPaletteEntry(
            command=f"/{name}",
            title=name,
            description=desc,
            category=category,
            usage=usage,
            keywords=(name, desc, category, usage),
        )
        for name, (_, desc, usage, category) in sorted(_COMMANDS.items())
    ]
    for skill in fw.list_skills():
        entries.append(
            CommandPaletteEntry(
                command=f"/skill {skill.skill_id}",
                title=skill.name or skill.skill_id,
                description=skill.description or "激活技能",
                category="技能",
                usage="/skill <id>",
                keywords=tuple(skill.trigger_keywords) + (skill.skill_id,),
            )
        )
    return entries


def score_palette_entry(query: str, entry: CommandPaletteEntry, recent_commands: Sequence[str] = ()) -> int:
    normalized = query.strip().lower()
    if not normalized:
        recency_bonus = 40 - recent_commands.index(entry.command) if entry.command in recent_commands else 0
        return 1 + max(recency_bonus, 0)

    haystack = " ".join(
        [
            entry.command,
            entry.title,
            entry.description,
            entry.category,
            entry.usage,
            *entry.keywords,
        ]
    ).lower()
    score = 0
    command_text = entry.command.lower()
    if command_text.startswith(normalized):
        score = 120
    elif normalized in command_text:
        score = 90
    elif normalized in haystack:
        score = 60
    else:
        compact_query = normalized.replace(" ", "")
        compact_command = command_text.replace(" ", "")
        if compact_query and all(char in compact_command for char in compact_query):
            score = 40
    if entry.command in recent_commands:
        score += max(10 - recent_commands.index(entry.command), 0)
    return score


def search_palette_entries(
    fw: AgentFramework,
    query: str,
    recent_commands: Sequence[str] = (),
) -> list[CommandPaletteEntry]:
    scored = [
        (score_palette_entry(query, entry, recent_commands), entry)
        for entry in build_palette_entries(fw)
    ]
    return [
        entry
        for score, entry in sorted(scored, key=lambda item: (-item[0], item[1].command))
        if score > 0
    ]


async def run_single_task(
    fw: AgentFramework,
    task: str,
    *,
    user_id: str | None = None,
    history: list[Message] | None = None,
) -> str:
    result = await fw.run(task, user_id=user_id, initial_session_messages=history)
    return format_result(result)


async def execute_slash_command(
    fw: AgentFramework,
    mock_model: InteractiveMockModel | None,
    state: ReplState,
    raw_command: str,
    *,
    ui_mode: str = "textual",
) -> CommandExecution:
    if raw_command == "/":
        return CommandExecution(output=_show_slash_menu(fw=fw), handled=True)

    command_line = raw_command[1:]
    parts = command_line.split(maxsplit=1)
    command_name = parts[0].lower() if parts else ""
    command_args = parts[1] if len(parts) > 1 else ""

    if command_name in ("exit", "quit", "q"):
        return CommandExecution(handled=True, should_exit=True)
    if command_name == "clear":
        if ui_mode == "classic":
            os.system("cls" if os.name == "nt" else "clear")
        return CommandExecution(handled=True, clear_output=(ui_mode != "classic"))

    if command_name in _COMMANDS:
        handler, _, _, _ = _COMMANDS[command_name]
        state.record_command(f"/{command_name}")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            await handler(fw, mock_model, state, command_args)
        return CommandExecution(output=buffer.getvalue().rstrip(), handled=True)

    matches = [name for name in _COMMANDS if name.startswith(command_name)]
    if matches:
        output = f"  {_red(f'未知命令: /{command_name}')}\n{_show_slash_menu(command_name, fw=fw)}"
        return CommandExecution(output=output, handled=True)
    return CommandExecution(
        output=f"  {_red(f'未知命令: /{command_name}')}  {_dim('输入 / 查看所有命令')}",
        handled=True,
    )


async def execute_user_input(
    fw: AgentFramework,
    mock_model: InteractiveMockModel | None,
    state: ReplState,
    user_input: str,
    cancel_event: asyncio.Event | None = None,
) -> str:
    if mock_model:
        mock_model._reset_turn()
    result = await fw.run(
        user_input,
        initial_session_messages=state.history,
        user_id=state.user_id,
        cancel_event=cancel_event,
    )
    output = format_result(result)
    if result.success:
        state.append_turn(user_input, result)
    return output


async def run_classic_repl(fw: AgentFramework, mock_model: InteractiveMockModel | None) -> None:
    state = ReplState()
    print(render_banner(mock_model is not None, None, fw))
    while True:
        try:
            user_input = input(f"{_bold(_green('> '))}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_dim('再见!')}")
            break
        if not user_input:
            continue
        if user_input.startswith("/"):
            execution = await execute_slash_command(fw, mock_model, state, user_input, ui_mode="classic")
            if execution.output:
                print(execution.output)
            if execution.should_exit:
                print(f"{_dim('再见!')}")
                break
            continue
        try:
            print(await execute_user_input(fw, mock_model, state, user_input))
        except Exception as exc:
            print(f"\n  {_red('运行错误:')}")
            print(f"  {exc}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()
    await fw.shutdown()


def should_use_mock(args: argparse.Namespace) -> bool:
    return bool(args.mock or args.config is None)


def format_missing_textual_message() -> str:
    return (
        "Textual 未安装，已回退到经典终端模式。\n"
        "安装依赖后可获得命令面板与更完整的交互界面：`pip install textual`。"
    )


def build_framework_from_args(args: argparse.Namespace) -> tuple[AgentFramework, InteractiveMockModel | None]:
    return build_framework(
        config_path=args.config,
        use_mock=should_use_mock(args),
        auto_approve=args.auto_approve,
        model_override=args.model,
    )


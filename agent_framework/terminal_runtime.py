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

from agent_framework.adapters.model.base_adapter import (BaseModelAdapter,
                                                         ModelChunk)
from agent_framework.entry import AgentFramework
from agent_framework.infra.config import load_config
from agent_framework.models.agent import Skill
from agent_framework.models.message import (Message, ModelResponse, TokenUsage,
                                            ToolCallRequest)
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
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[ModelChunk]:
        response = await self.complete(messages, tools, temperature, max_tokens)
        # Simulate token-by-token streaming for mock
        content = response.content or ""
        for i in range(0, len(content), 4):
            chunk_text = content[i:i + 4]
            yield ModelChunk(delta_content=chunk_text)
            await asyncio.sleep(0.01)
        yield ModelChunk(
            finish_reason=response.finish_reason,
            delta_tool_calls=(
                [{"index": j, "id": tc.id,
                  "function": {"name": tc.function_name,
                               "arguments": __import__("json").dumps(tc.arguments)}}
                 for j, tc in enumerate(response.tool_calls)]
                if response.tool_calls else None
            ),
        )

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(message.content or "") // 4 for message in messages)

    def supports_parallel_tool_calls(self) -> bool:
        return True


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

BUILTIN_SKILLS = [
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
    parser.add_argument("--team", action="store_true", help="启动 Agent Team 模式")
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
    use_mock_final = use_mock or (config_path is None and not config.model.api_key)
    if use_mock_final:
        mock_model = InteractiveMockModel()
        # Mock模式下直接传入adapter，避免初始化不必要的默认适配器
        framework.setup(auto_approve_tools=auto_approve, model_adapter=mock_model)
    else:
        framework.setup(auto_approve_tools=auto_approve)

    framework.register_tool(weather)
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
        self.total_tokens_estimate = 0
        self.conversation_id: str | None = None

    def append_turn(self, user_input: str, result: Any) -> None:
        from agent_framework.agent.message_projector import MessageProjector

        user_message = Message(role="user", content=user_input)
        self.history.append(user_message)
        self.total_tokens_estimate += self._estimate_message_tokens(user_message)
        if getattr(result, "iteration_history", None):
            for iteration in result.iteration_history:
                for message in MessageProjector.project_iteration(iteration):
                    projected = Message(
                        role=message.role,
                        content=message.content,
                        tool_calls=message.tool_calls,
                        tool_call_id=message.tool_call_id,
                        name=message.name,
                    )
                    self.history.append(projected)
                    self.total_tokens_estimate += self._estimate_message_tokens(projected)
        else:
            assistant = Message(role="assistant", content=result.final_answer or "")
            self.history.append(assistant)
            self.total_tokens_estimate += self._estimate_message_tokens(assistant)
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

        from agent_framework.context.summarizer import (call_llm_compress,
                                                        is_summary_message,
                                                        messages_to_text,
                                                        wrap_summary)

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
        self.total_tokens_estimate = new_tokens
        return f"Compacted {old_count} messages (~{old_tokens} tokens) -> 1 summary (~{new_tokens} tokens)"

    def record_command(self, command_name: str) -> None:
        if command_name in self.recent_commands:
            self.recent_commands.remove(command_name)
        self.recent_commands.insert(0, command_name)
        del self.recent_commands[8:]

    def clear(self) -> None:
        self.history.clear()
        self.turn_count = 0
        self.total_tokens_estimate = 0

    def message_count(self) -> int:
        return len(self.history)

    def _estimate_tokens(self) -> int:
        total = 0
        for message in self.history:
            total += self._estimate_message_tokens(message)
        return max(total, 1)

    def save_to_db(self, store: Any, project_id: str) -> None:
        """持久化当前会话到 SQLiteMemoryStore。"""
        if not self.history or not self.conversation_id:
            return
        store.save_conversation(project_id, self.conversation_id, self.history)

    def load_from_db(self, store: Any, project_id: str) -> int:
        """从 SQLiteMemoryStore 恢复最近一次会话，返回加载的消息数。"""
        conv_id = store.get_latest_conversation_id(project_id)
        if not conv_id:
            self.conversation_id = store.new_conversation_id()
            return 0
        messages = store.load_conversation(conv_id)
        if not messages:
            self.conversation_id = store.new_conversation_id()
            return 0
        self.conversation_id = conv_id
        self.history = messages
        self.total_tokens_estimate = sum(
            self._estimate_message_tokens(m) for m in messages
        )
        self.turn_count = sum(1 for m in messages if m.role == "user")
        return len(messages)

    def new_context(self, store: Any, project_id: str) -> None:
        """保存当前会话后开启新上下文窗口。旧会话保留在 DB。"""
        self.save_to_db(store, project_id)
        self.clear()
        self.conversation_id = store.new_conversation_id()

    def switch_conversation(self, store: Any, project_id: str, target_conv_id: str) -> int:
        """保存当前会话，切换到指定 conversation_id，返回加载的消息数。"""
        self.save_to_db(store, project_id)
        messages = store.load_conversation(target_conv_id)
        self.clear()
        self.conversation_id = target_conv_id
        if not messages:
            return 0
        self.history = messages
        self.total_tokens_estimate = sum(
            self._estimate_message_tokens(m) for m in messages
        )
        self.turn_count = sum(1 for m in messages if m.role == "user")
        return len(messages)

    def render_history_summary(self, max_turns: int = 3) -> str:
        """渲染最近几轮 user/assistant 摘要，用于恢复后显示。"""
        pairs: list[tuple[str, str]] = []
        current_user = ""
        for msg in self.history:
            if msg.role == "user":
                current_user = (msg.content or "")[:80]
            elif msg.role == "assistant" and current_user:
                pairs.append((current_user, (msg.content or "")[:80]))
                current_user = ""
        if not pairs:
            return ""
        recent = pairs[-max_turns:]
        lines = []
        for user_text, agent_text in recent:
            lines.append(f"    {_dim('User:')} {_dim(user_text)}")
            lines.append(f"    {_dim('Agent:')} {_dim(agent_text)}")
        if len(pairs) > max_turns:
            lines.insert(0, f"    {_dim(f'... 省略 {len(pairs) - max_turns} 轮 ...')}")
        return "\n".join(lines)

    @staticmethod
    def _estimate_message_tokens(message: Message) -> int:
        text = message.text_content or ""
        cjk_chars = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        ascii_chars = len(text) - cjk_chars
        tokens = ascii_chars // 4 + int(cjk_chars / 1.5)
        # Multimodal parts: estimate extra tokens for non-text content
        if message.content_parts:
            for p in message.content_parts:
                if p.type == "text":
                    continue
                if p.data:
                    tokens += len(p.data) // 4
                else:
                    tokens += 85
        return tokens


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
    print(f"  {_bold(_yellow('[ Tool Examples ]'))}")
    print(f"    {_dim('持久任务图: task_create → task_update(in_progress/completed) → task_list/task_get')}")
    print(f"    {_dim('后台 shell 任务: bash_exec(run_in_background=True) → bash_output(task_id) → task_stop(task_id)')}")
    print(f"    {_dim('注意: task_stop 只用于后台 shell 的字符串 task_id，不用于持久任务图的整数 task_id。')}")
    print()
    print(f"  {_dim('直接输入文本与 Agent 对话。所有命令均以 / 开头。')}")


async def _execute_tool(fw: AgentFramework, tool_name: str, arguments: dict) -> Any:
    """Route a CLI command through ToolExecutor.execute() for unified execution."""
    from agent_framework.models.tool import ToolResult
    if not fw._deps or not fw._deps.tool_executor:
        return ToolResult(tool_call_id="cli", tool_name=tool_name, success=False,
                          output="Framework not initialized")
    import uuid
    req = ToolCallRequest(id=f"cli_{uuid.uuid4().hex[:8]}", function_name=tool_name, arguments=arguments)
    result, _meta = await fw._deps.tool_executor.execute(req)
    return result


@_register_cmd("exit", "退出程序", category="通用")
async def _cmd_exit(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    return


@_register_cmd("reset", "新建上下文窗口（当前会话保存到 DB，开启全新对话）", category="通用")
async def _cmd_reset(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    from pathlib import Path
    if fw._memory_store:
        state.new_context(fw._memory_store, Path.cwd().name)
    else:
        state.clear()
    if mock:
        mock._reset_turn()
    print(f"  {_green('已开启新上下文窗口')} ({_dim(f'conversation: {state.conversation_id[:8]}...')})")


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
    result = await _execute_tool(fw, "list_memories", {"user_id": state.user_id})
    if not result.success:
        print(f"    {_dim('(记忆系统未初始化)')}")
        return
    records = result.output if isinstance(result.output, list) else []
    print(f"\n  {_bold('已保存记忆')} ({len(records)}):\n")
    for index, record in enumerate(records):
        kind = _cyan(f"[{record.get('kind', '')}]")
        pinned = _yellow(" [pinned]") if record.get("pinned") else ""
        active = "" if record.get("active", True) else _dim(" (inactive)")
        print(f"    {_dim(f'#{index}')} {kind} {record.get('title', '')}{pinned}{active}")
        content = record.get("content", "")
        if content:
            print(f"       {_dim(content[:80])}")
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
            text = message.text_content or ""
            suffix = ""
            if message.has_multimodal:
                media_types = [p.type for p in (message.content_parts or []) if p.type != "text"]
                suffix = f" {_magenta('[+' + ', '.join(media_types) + ']')}"
            print(f"  {_cyan('User:')} {text[:120]}{suffix}")
        elif message.role == "assistant":
            print(f"  {_green('Agent:')} {(message.text_content or '')[:120]}")
            if index < len(messages) - 1:
                print()
    print(f"\n  {_dim(f'消息数: {state.message_count()}  |  ~{estimate} tokens')}")


@_register_cmd("history-clear", "清空当前上下文的对话历史（含 DB）", category="会话")
async def _cmd_history_clear(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    if fw._memory_store and state.conversation_id:
        fw._memory_store.clear_conversation(state.conversation_id)
    state.clear()
    print(f"  {_green('当前上下文历史已清空')}")


@_register_cmd("sessions", "列出当前项目的所有会话窗口", category="会话")
async def _cmd_sessions(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    from pathlib import Path
    if not fw._memory_store:
        print(f"  {_dim('(记忆存储未启用)')}")
        return
    # 先保存当前会话再列出
    state.save_to_db(fw._memory_store, Path.cwd().name)
    convs = fw._memory_store.list_conversations(Path.cwd().name)
    if not convs:
        print(f"  {_dim('(无历史会话)')}")
        return
    print(f"\n  {_bold('会话列表')} ({len(convs)} 个):\n")
    for conv in convs:
        cid = conv["conversation_id"]
        short_id = cid[:8]
        is_current = cid == state.conversation_id
        marker = _green(" ◀ 当前") if is_current else ""
        print(f"    {_cyan(short_id)}  {conv['message_count']:3d} 条  {_dim(conv['created_at'][:19])}  {conv['preview'][:40]}{marker}")
    print(f"\n  {_dim('使用 /session-switch <id前缀> 切换会话')}")


@_register_cmd("session-switch", "切换到指定会话窗口", usage="/session-switch <id前缀>", category="会话")
async def _cmd_session_switch(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    from pathlib import Path
    prefix = args.strip()
    if not prefix:
        print(f"  {_red('请提供会话 ID 前缀')}，使用 /sessions 查看列表")
        return
    if not fw._memory_store:
        print(f"  {_dim('(记忆存储未启用)')}")
        return
    project_id = Path.cwd().name
    convs = fw._memory_store.list_conversations(project_id)
    matches = [c for c in convs if c["conversation_id"].startswith(prefix)]
    if not matches:
        print(f"  {_red('未找到匹配的会话:')} {prefix}")
        return
    if len(matches) > 1:
        print(f"  {_red('前缀不唯一，匹配到 ' + str(len(matches)) + ' 个会话:')}")
        for c in matches:
            print(f"    {_cyan(c['conversation_id'][:8])}  {c['preview'][:40]}")
        return
    target_id = matches[0]["conversation_id"]
    if target_id == state.conversation_id:
        print(f"  {_dim('已在当前会话中')}")
        return
    loaded = state.switch_conversation(fw._memory_store, project_id, target_id)
    print(f"  {_green(f'已切换到会话 {target_id[:8]}...')} ({loaded} 条消息)")
    summary = state.render_history_summary()
    if summary:
        print(summary)


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
        print(f"  {_dim('示例:')} /call weather {{\"city\": \"北京\"}}")
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
    result = await _execute_tool(fw, "clear_memories", {"user_id": state.user_id})
    print(f"  {_green(result.output if result.success else '清除失败')}")


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


@_register_cmd("checkpoints", "列出可恢复的快照", usage="/checkpoints [spawn_id]", category="会话")
async def _cmd_checkpoints(
    fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str,
) -> None:
    runtime = getattr(fw._deps, "sub_agent_runtime", None) if hasattr(fw, "_deps") else None
    store = getattr(runtime, "_checkpoint_store", None) if runtime else None
    if store is None:
        print(f"  {_yellow('快照未启用 (无 checkpoint store)')}")
        return

    spawn_id = args.strip() or f"repl_{state.conversation_id[:12]}"
    checkpoints = store.list_checkpoints(spawn_id)
    if not checkpoints:
        print(f"  {_dim(f'暂无快照 (spawn: {spawn_id})')}")
        return

    print(f"\n  {_bold(_yellow(f'快照列表 ({spawn_id}):'))}")
    for ckpt in checkpoints[:10]:
        cid = ckpt["checkpoint_id"]
        idx = ckpt["iteration_index"]
        summary = ckpt["summary"][:60]
        ts = ckpt["created_at"][:19]
        print(f"    {_cyan(cid)}  iter={idx}  {ts}  {_dim(summary)}")
    print()


@_register_cmd("resume", "从快照恢复对话", usage="/resume [checkpoint_id]", category="会话")
async def _cmd_resume(
    fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str,
) -> None:
    runtime = getattr(fw._deps, "sub_agent_runtime", None) if hasattr(fw, "_deps") else None
    store = getattr(runtime, "_checkpoint_store", None) if runtime else None
    if store is None:
        print(f"  {_yellow('快照未启用 (无 checkpoint store)')}")
        return

    checkpoint_id = args.strip() or None
    spawn_id = f"repl_{state.conversation_id[:12]}"

    # Load checkpoint
    if checkpoint_id:
        ckpt = store.load_by_id(checkpoint_id)
    else:
        ckpt = store.load_latest(spawn_id)

    if ckpt is None:
        print(f"  {_red('未找到快照')} {_dim(f'(spawn: {spawn_id})')}")
        print(f"  {_dim('使用 /checkpoints 查看可用快照')}")
        return

    # Restore session messages into REPL state
    restored_session = ckpt.restore_session_state()
    restored_msgs = restored_session.get_messages()

    state.history.clear()
    state.history.extend(restored_msgs)
    state.turn_count = ckpt.iteration_index

    print(f"  {_green('已恢复快照:')}")
    print(f"    ID:    {_cyan(ckpt.checkpoint_id)}")
    print(f"    轮次:  {ckpt.iteration_index}")
    print(f"    消息:  {len(restored_msgs)} 条")
    print(f"    摘要:  {ckpt.summary[:80]}")
    print()

    # Show last few messages as context
    tail = restored_msgs[-4:] if len(restored_msgs) > 4 else restored_msgs
    for msg in tail:
        role_tag = _green("You") if msg.role == "user" else _cyan("Agent")
        content_preview = (msg.content or "")[:100]
        print(f"    {_dim(role_tag + ':')} {content_preview}")
    print(f"\n  {_dim('对话已恢复，继续输入即可。')}")


@_register_cmd("team-start", "启动 Agent Team 模式", usage="/team-start [team_name]", category="团队")
async def _cmd_team_start(
    fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str,
) -> None:
    if hasattr(state, "_team_coordinator") and state._team_coordinator is not None:
        print(f"  {_yellow('Team 已启动')}: {state._team_coordinator.team_id}")
        return
    team_name = args.strip() or "default_team"
    try:
        team_env = _setup_team(fw, team_name)
        state._team_coordinator = team_env["coordinator"]
        state._team_mailbox = team_env["mailbox"]
        state._team_registry = team_env["registry"]
        print(f"  {_green('Team 已启动')}: {team_name}")
        print(f"    team_id: {state._team_coordinator.team_id}")
        print(f"    工具: team(action=...) + mail(action=...)")
        print(f"    示例: team(action='spawn', role='coder', task='fix bug')")
    except Exception as e:
        print(f"  {_red(f'Team 启动失败: {e}')}")


@_register_cmd("team-status", "查看团队状态", category="团队")
async def _cmd_team_status(
    fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str,
) -> None:
    coord = getattr(state, "_team_coordinator", None)
    if coord is None:
        print(f"  {_yellow('Team 未启动，使用 /team-start 启动')}")
        return
    status = coord.get_team_status()
    tid = status["team_id"]
    print(f"\n  {_bold(_yellow(f'Team: {tid}'))}")
    print(f"    Lead: {status['lead']}")
    print(f"    成员: {status['member_count']}")
    for m in status["members"]:
        role_color = _green if m["role"] == "lead" else _cyan
        print(f"      {role_color(m['agent_id'])} ({m['role']}) — {m['status']}")
    if status["pending_plans"]:
        print(f"    待审计划: {status['pending_plans']}")
    if status["pending_shutdowns"]:
        print(f"    待关闭: {status['pending_shutdowns']}")
    print()


@_register_cmd("team-inbox", "查看 Lead 收件箱", category="团队")
async def _cmd_team_inbox(
    fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str,
) -> None:
    coord = getattr(state, "_team_coordinator", None)
    if coord is None:
        print(f"  {_yellow('Team 未启动')}")
        return
    processed = coord.process_inbox()
    if not processed:
        print(f"  {_dim('收件箱为空')}")
        return
    print(f"\n  {_bold(_yellow(f'处理了 {len(processed)} 条事件:'))}")
    for evt in processed:
        print(f"    [{evt.get('type', '?')}] from={evt.get('from', '?')} {_dim(str({k: v for k, v in evt.items() if k not in ('type', 'from')}))}")
    print()


@_register_cmd("demo", "运行内置演示场景", usage="/demo [weather|skill]", category="演示")
async def _cmd_demo(fw: AgentFramework, mock: InteractiveMockModel | None, state: ReplState, args: str) -> None:
    demos = {
        "weather": "查询北京和上海的天气",
        "skill": "请审查这段 Python 代码的可读性和潜在问题: def add(a,b): return a+b",
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
    for demo_name in ["weather", "skill"]:
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


def _print_result(result: Any, include_trace: bool = True) -> None:
    if include_trace and getattr(result, "iteration_history", None):
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

    # Progressive intermediate responses — display like conversation turns
    progressive = getattr(result, "progressive_responses", [])
    if progressive:
        print(f"\n  {_bold(_yellow('Progressive 中间回复:'))}")
        for pi, resp in enumerate(progressive, 1):
            print(f"  {_dim(f'[{pi}/{len(progressive)}]')} {_cyan('Agent:')}")
            for line in resp.splitlines():
                print(f"    {line}")
            print()

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


def format_result(result: Any, include_trace: bool = True) -> str:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        _print_result(result, include_trace=include_trace)
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
    include_trace: bool = True,
) -> str:
    if mock_model:
        mock_model._reset_turn()
    result = await fw.run(
        user_input,
        initial_session_messages=state.history,
        user_id=state.user_id,
        cancel_event=cancel_event,
    )
    output = format_result(result, include_trace=include_trace)
    if result.success:
        state.append_turn(user_input, result)
    return output


async def execute_user_input_stream(
    fw: AgentFramework,
    mock_model: InteractiveMockModel | None,
    state: ReplState,
    user_input: str,
    cancel_event: asyncio.Event | None = None,
):
    """Streaming variant — yields StreamEvents in real-time.

    The consumer (TUI) renders tokens incrementally. The final DONE event
    carries the AgentRunResult for state bookkeeping.
    """
    from agent_framework.models.stream import StreamEvent, StreamEventType

    if mock_model:
        mock_model._reset_turn()

    async for event in fw.run_stream(
        user_input,
        initial_session_messages=state.history,
        user_id=state.user_id,
        cancel_event=cancel_event,
    ):
        if event.type == StreamEventType.DONE:
            result = event.data.get("result")
            if result and result.success:
                state.append_turn(user_input, result)
        yield event


async def _setup_protocols(fw: AgentFramework) -> None:
    """连接配置中的 MCP servers 和 A2A agents。"""
    if fw.config.mcp.servers:
        try:
            await fw.setup_mcp()
            count = len(fw._mcp_manager.list_connected_servers()) if fw._mcp_manager else 0
            print(f"  {_green(f'MCP: 已连接 {count} 个服务')}")
        except Exception as e:
            print(f"  {_red(f'MCP 连接失败: {e}')}")
    if fw.config.a2a.known_agents:
        try:
            await fw.setup_a2a()
            count = len(fw._a2a_adapter.list_known_agents()) if fw._a2a_adapter else 0
            print(f"  {_green(f'A2A: 已发现 {count} 个远程 Agent')}")
        except Exception as e:
            print(f"  {_red(f'A2A 连接失败: {e}')}")


async def _execute_with_progressive(
    fw: AgentFramework,
    mock_model: InteractiveMockModel | None,
    state: ReplState,
    user_input: str,
) -> str:
    """Execute user input with real-time progressive event display."""
    from agent_framework.models.stream import StreamEventType

    if mock_model:
        mock_model._reset_turn()

    result = None
    has_progressive = False
    progressive_tool_call_ids: set[str] = set()

    async for event in fw.run_stream(
        user_input,
        initial_session_messages=state.history,
        user_id=state.user_id,
    ):
        if event.type == StreamEventType.TOKEN:
            print(event.data.get("text", ""), end="", flush=True)

        elif event.type == StreamEventType.TOOL_CALL_START:
            tool_name = event.data.get("tool_name", "?")
            print(f"\n  {_dim('[tool]')} {_cyan(tool_name)}", end="", flush=True)

        elif event.type == StreamEventType.TOOL_CALL_DONE:
            tool_call_id = str(event.data.get("tool_call_id", ""))
            if tool_call_id in progressive_tool_call_ids:
                pass  # Suppress — PROGRESSIVE_DONE handles display
            else:
                success = event.data.get("success", False)
                marker = _green(" [ok]") if success else _red(" [fail]")
                print(marker, flush=True)

        elif event.type == StreamEventType.ITERATION_START:
            idx = event.data.get("iteration_index", 0)
            if idx > 0:
                print(f"\n  {_dim(f'--- iter #{idx + 1}')}")

        elif event.type == StreamEventType.PROGRESSIVE_START:
            tool_call_id = str(event.data.get("tool_call_id", ""))
            if tool_call_id:
                progressive_tool_call_ids.add(tool_call_id)
            idx = event.data.get("index", 0)
            total = event.data.get("total", 0)
            tool_name = event.data.get("tool_name", "")
            description = event.data.get("description", "")[:60]
            tag = "subagent" if tool_name == "spawn_agent" else "tool"
            print(f"  {_dim(f'[{tag} {idx}/{total}]')} {_yellow('启动:')} {description}")
            has_progressive = True

        elif event.type == StreamEventType.PROGRESSIVE_DONE:
            idx = event.data.get("index", 0)
            total = event.data.get("total", 0)
            tool_name = event.data.get("tool_name", "")
            success = event.data.get("success", False)
            description = event.data.get("description", "")
            # Prefer display_text (human-readable) over raw output
            display = event.data.get("display_text") or event.data.get("output", "")
            status = _green("完成") if success else _red("失败")
            tag = "subagent" if tool_name == "spawn_agent" else "tool"
            if tag == "subagent" and description:
                print(f"  {_dim(f'[{tag} {idx}/{total}]')} {status}: {description}")
                if display:
                    for line in display.strip().split("\n"):
                        print(f"    {line}")
            else:
                print(f"  {_dim(f'[{tag} {idx}/{total}]')} {status}: {display}")

        elif event.type == StreamEventType.SUBAGENT_STREAM:
            inner = event.data.get("event_type", "")
            sid = event.data.get("spawn_id", "")[:8]
            if inner == "token":
                text = event.data.get("text", "")
                # Track newlines for prefix insertion
                if not hasattr(state, "_subagent_at_newline"):
                    state._subagent_at_newline = True
                if state._subagent_at_newline:
                    print(f"  {_dim('│')} ", end="", flush=True)
                    state._subagent_at_newline = False
                for ch in text:
                    if ch == "\n":
                        print(flush=True)
                        print(f"  {_dim('│')} ", end="", flush=True)
                    else:
                        print(ch, end="", flush=True)
            elif inner == "tool_call_start":
                tool_name = event.data.get("tool_name", "?")
                print(f"\n  {_dim('│')} {_dim('[tool]')} {_cyan(tool_name)}", end="", flush=True)
                state._subagent_at_newline = False
            elif inner == "tool_call_done":
                success = event.data.get("success", False)
                marker = _green(" [ok]") if success else _red(" [fail]")
                print(marker, flush=True)
                state._subagent_at_newline = True
            elif inner == "iteration_start":
                idx = event.data.get("iteration_index", 0)
                if idx > 0:
                    print(f"\n  {_dim('│')} {_dim(f'--- iter #{idx + 1}')}")
                    state._subagent_at_newline = True

        elif event.type == StreamEventType.DONE:
            result = event.data.get("result")
            if result and result.success:
                state.append_turn(user_input, result)

    if result is None:
        return f"  {_red('运行异常: 未收到结果')}"

    if has_progressive:
        # Progressive events already shown — just show final answer
        if result.success:
            lines = [f"\n  {_green('Agent 最终回复:')}"]
            lines.append(f"  {'─' * 56}")
            for line in (result.final_answer or "(无回答)").splitlines():
                lines.append(f"  {line}")
            lines.append(f"  {'─' * 56}")
            parts = [f"迭代: {result.iterations_used}", f"Tokens: {result.usage.total_tokens}"]
            lines.append(f"  {_dim(' | '.join(parts))}")
            return "\n".join(lines)
        return f"  {_red('Agent 错误:')} {result.error}"

    # Non-progressive: use standard format
    return format_result(result, include_trace=True)


def _setup_team(fw: AgentFramework, team_name: str) -> dict:
    """Initialize Agent Team components and wire into framework."""
    import uuid
    from agent_framework.notification.bus import AgentBus
    from agent_framework.notification.persistence import InMemoryBusPersistence, SQLiteBusPersistence
    from agent_framework.team.registry import TeamRegistry
    from agent_framework.team.plan_registry import PlanRegistry
    from agent_framework.team.shutdown_registry import ShutdownRegistry
    from agent_framework.team.mailbox import TeamMailbox
    from agent_framework.team.coordinator import TeamCoordinator

    team_id = f"team_{uuid.uuid4().hex[:8]}"

    # Choose bus backend
    team_cfg = fw.config.team
    if team_cfg.bus_backend == "sqlite":
        persistence = SQLiteBusPersistence(db_path=team_cfg.bus_db_path)
    else:
        persistence = InMemoryBusPersistence()

    bus = AgentBus(persistence=persistence)
    registry = TeamRegistry(team_id)
    plan_reg = PlanRegistry()
    shutdown_reg = ShutdownRegistry()
    mailbox = TeamMailbox(bus, registry)

    lead_id = getattr(fw._agent, "agent_id", "lead") if hasattr(fw, "_agent") else "lead"
    runtime = getattr(fw._deps, "sub_agent_runtime", None) if hasattr(fw, "_deps") else None

    coordinator = TeamCoordinator(
        team_id=team_id,
        lead_agent_id=lead_id,
        mailbox=mailbox,
        team_registry=registry,
        plan_registry=plan_reg,
        shutdown_registry=shutdown_reg,
        sub_agent_runtime=runtime,
    )
    coordinator.create_team(team_name)

    # Wire team tools into tool executor
    if hasattr(fw, "_deps") and hasattr(fw._deps, "tool_executor"):
        executor = fw._deps.tool_executor
        executor._team_coordinator = coordinator
        executor._team_mailbox = mailbox
        executor._current_agent_role = "lead"
        executor._current_team_id = team_id
        executor._current_spawn_id = lead_id

    return {
        "bus": bus,
        "registry": registry,
        "plan_registry": plan_reg,
        "shutdown_registry": shutdown_reg,
        "mailbox": mailbox,
        "coordinator": coordinator,
    }


def _auto_checkpoint(fw: AgentFramework, state: ReplState, user_input: str) -> None:
    """Save a checkpoint after each terminal user interaction.

    Only triggers at real user input boundaries. Silent on failure —
    checkpointing is optional and must not interrupt the REPL.
    """
    try:
        runtime = getattr(fw._deps, "sub_agent_runtime", None) if hasattr(fw, "_deps") else None
        if runtime is None or getattr(runtime, "_checkpoint_store", None) is None:
            return

        from agent_framework.models.agent import AgentState
        from agent_framework.models.session import SessionState

        # Build lightweight state snapshot from REPL history
        session = SessionState(
            session_id=state.conversation_id,
            run_id=f"repl_turn_{state.turn_count}",
        )
        for msg in state.history:
            session.append_message(msg)

        agent_state = AgentState(
            run_id=session.run_id,
            task=user_input,
            iteration_count=state.turn_count,
        )

        runtime._checkpoint_store.save(
            spawn_id=f"repl_{state.conversation_id[:12]}",
            agent_state=agent_state,
            session_state=session,
            summary=f"Turn {state.turn_count}: {user_input[:80]}",
            trigger="user_input",
        )
    except Exception:
        pass  # Checkpoint failure must not break REPL


async def run_classic_repl(fw: AgentFramework, mock_model: InteractiveMockModel | None) -> None:
    import uuid
    from pathlib import Path
    project_id = Path.cwd().name
    state = ReplState()

    # 1) 连接 MCP/A2A（在 banner 之前，工具数需要反映在 banner 中）
    await _setup_protocols(fw)

    # 2) Banner
    print(render_banner(mock_model is not None, None, fw))

    # 3) 恢复历史（banner 之后显示，避免与 banner 重叠）
    if fw._memory_store:
        loaded = state.load_from_db(fw._memory_store, project_id)
        if loaded:
            print(f"  {_dim(f'已恢复 {loaded} 条历史消息 (conv: {state.conversation_id[:8]}...)')}")
            summary = state.render_history_summary()
            if summary:
                print(summary)
            print()
    else:
        state.conversation_id = str(uuid.uuid4())
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
            output = await _execute_with_progressive(fw, mock_model, state, user_input)
            print(output)
            # Auto-checkpoint after each successful user interaction
            _auto_checkpoint(fw, state, user_input)
        except Exception as exc:
            print(f"\n  {_red('运行错误:')}")
            print(f"  {exc}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()
    # 退出时持久化会话历史
    if fw._memory_store:
        state.save_to_db(fw._memory_store, project_id)
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

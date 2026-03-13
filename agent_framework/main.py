"""Interactive terminal interface for manual testing of the Agent Framework.

All commands start with ``/``. Plain text is sent to the Agent for conversation.

Usage:
    python -m agent_framework.main                              # Mock (offline)
    python -m agent_framework.main --config config/doubao.json  # Real model
    python -m agent_framework.main --mock                       # Force mock
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sys
import traceback
from typing import AsyncIterator, Any

# Enable readline for proper line editing (CJK backspace, history, etc.)
try:
    import readline  # noqa: F401
except ImportError:
    pass  # Windows fallback — pyreadline3 optional

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_NO_COLOR = os.environ.get("NO_COLOR") is not None


def _c(code: str, text: str) -> str:
    if _NO_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def _bold(t: str) -> str:  return _c("1", t)
def _dim(t: str) -> str:   return _c("2", t)
def _green(t: str) -> str:  return _c("32", t)
def _yellow(t: str) -> str: return _c("33", t)
def _cyan(t: str) -> str:   return _c("36", t)
def _red(t: str) -> str:    return _c("31", t)
def _magenta(t: str) -> str: return _c("35", t)


def _rl_wrap(ansi: str) -> str:
    """Wrap ANSI escape for readline: mark non-printable spans so readline
    doesn't count them toward cursor width (fixes CJK backspace artifacts).

    ``\\x01`` = RL_PROMPT_START_IGNORE, ``\\x02`` = RL_PROMPT_END_IGNORE.
    Only needed inside strings passed to ``input(prompt)``.
    """
    if _NO_COLOR:
        return ansi
    import re
    return re.sub(r'(\033\[[0-9;]*m)', lambda m: f'\x01{m.group(1)}\x02', ansi)
# ---------------------------------------------------------------------------
# Mock model
# ---------------------------------------------------------------------------

from agent_framework.adapters.model.base_adapter import BaseModelAdapter, ModelChunk
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest


class InteractiveMockModel(BaseModelAdapter):
    """Keyword-based mock LLM for offline testing."""

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

        new_msgs = messages[self._last_seen_msg_count:]
        self._last_seen_msg_count = len(messages)
        for m in new_msgs:
            if m.role == "tool" and m.content:
                self._tool_results.append(m.content)

        if self._tool_results and self._call_count > 1:
            summary = "\n".join(f"  - {r}" for r in self._tool_results)
            return ModelResponse(
                content=f"[Mock] 工具执行完成:\n{summary}",
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
            )

        user_input = ""
        for m in messages:
            if m.role == "user":
                user_input = m.content or ""

        tool_names = {t["function"]["name"] for t in (tools or [])}
        lo = user_input.lower()
        calls: list[ToolCallRequest] = []

        if "calculator" in tool_names and any(k in lo for k in ("计算", "算", "calc", "+", "*", "/")):
            expr = "42 * 13 + 7"
            for p in user_input.split():
                if any(ch in p for ch in "0123456789+-*/."):
                    expr = p
                    break
            calls.append(ToolCallRequest(id="tc_calc", function_name="calculator", arguments={"expression": expr}))

        if "weather" in tool_names and any(k in lo for k in ("天气", "weather")):
            city = "北京"
            for c in ("北京", "上海", "深圳", "东京", "广州"):
                if c in user_input:
                    city = c
                    break
            calls.append(ToolCallRequest(id="tc_weather", function_name="weather", arguments={"city": city}))

        if "note" in tool_names and any(k in lo for k in ("笔记", "记录", "note")):
            calls.append(ToolCallRequest(id="tc_note", function_name="note", arguments={"title": "用户笔记", "content": user_input}))

        if "read_file" in tool_names and any(k in lo for k in ("读文件", "read file", "cat ")):
            path = user_input.split()[-1] if len(user_input.split()) > 1 else "."
            calls.append(ToolCallRequest(id="tc_read", function_name="read_file", arguments={"path": path}))

        if "list_directory" in tool_names and any(k in lo for k in ("目录", "ls", "list dir")):
            path = user_input.split()[-1] if len(user_input.split()) > 1 else "."
            calls.append(ToolCallRequest(id="tc_ls", function_name="list_directory", arguments={"path": path}))

        if "run_command" in tool_names and any(k in lo for k in ("执行命令", "运行命令", "shell")):
            cmd = user_input.split(maxsplit=1)[-1] if " " in user_input else "echo hello"
            calls.append(ToolCallRequest(id="tc_cmd", function_name="run_command", arguments={"command": cmd}))

        if calls:
            return ModelResponse(
                content="[Mock] 识别意图，调用工具...",
                tool_calls=calls, finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=40, completion_tokens=20, total_tokens=60),
            )

        return ModelResponse(
            content=f"[Mock] 收到: {user_input}\n(使用 --config 连接真实模型)",
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
# Demo tools & skills
# ---------------------------------------------------------------------------

from agent_framework.tools.decorator import tool
from agent_framework.models.agent import Skill


@tool(name="calculator", description="计算数学表达式", category="math")
def calculator(expression: str) -> str:
    """安全地计算数学表达式。"""
    allowed = set("0123456789+-*/.() ")
    if not all(c in allowed for c in expression):
        return f"错误：表达式包含非法字符: {expression}"
    try:
        result = eval(expression)  # demo only
        return f"{expression} = {result}"
    except Exception as e:
        return f"计算错误: {e}"


@tool(name="weather", description="查询城市天气（模拟数据）", category="info")
def weather(city: str) -> str:
    """查询指定城市的天气信息。"""
    fake_data = {
        "北京": "晴天, 28°C, 湿度 40%", "上海": "多云, 25°C, 湿度 65%",
        "深圳": "阵雨, 30°C, 湿度 80%", "东京": "晴天, 22°C, 湿度 55%",
        "广州": "多云, 32°C, 湿度 75%",
    }
    return fake_data.get(city, f"未找到 {city} 的天气数据")


@tool(name="note", description="保存一条笔记", category="util")
def note(title: str, content: str) -> str:
    """保存笔记。"""
    return f"已保存笔记 [{title}]: {content}"


BUILTIN_SKILLS = [
    Skill(
        skill_id="math_expert", name="数学专家",
        description="激活数学专家模式，提供详细计算步骤",
        trigger_keywords=["数学", "计算", "math", "calculate"],
        system_prompt_addon="你是一位数学专家。请用清晰的步骤解释计算过程，给出精确结果。",
    ),
    Skill(
        skill_id="translator", name="翻译助手",
        description="激活翻译模式，进行中英互译",
        trigger_keywords=["翻译", "translate", "英译中", "中译英"],
        system_prompt_addon="你是一位专业翻译。请准确翻译用户的文本，保持原文风格和语气。",
    ),
    Skill(
        skill_id="code_reviewer", name="代码审查",
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
    import logging
    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    from agent_framework.entry import AgentFramework
    from agent_framework.infra.config import load_config

    config = load_config(config_path)
    mock_model = None
    framework = AgentFramework(config=config)

    if use_mock or (config_path is None and not config.model.api_key):
        mock_model = InteractiveMockModel()
        framework.setup(auto_approve_tools=auto_approve)
        framework._deps.model_adapter = mock_model
    else:
        framework.setup(auto_approve_tools=auto_approve)

    framework.register_tool(calculator)
    framework.register_tool(weather)
    framework.register_tool(note)
    for skill in BUILTIN_SKILLS:
        framework.register_skill(skill)

    return framework, mock_model


# ---------------------------------------------------------------------------
# REPL State — shared between REPL loop and command handlers
# ---------------------------------------------------------------------------

MAX_HISTORY_TOKENS = 4096  # token budget for conversation history


class ReplState:
    """Mutable state shared across REPL loop and command handlers."""

    def __init__(self) -> None:
        self.history: list[Message] = []
        self.turn_count: int = 0

    def append_turn(self, user_input: str, result: Any) -> None:
        """Record a complete turn (user + assistant + tool calls/results)."""
        self.history.append(Message(role="user", content=user_input))

        # Extract tool interactions from iteration history if available
        if hasattr(result, "iterations_used") and result.iterations_used > 0:
            # For multi-iteration runs, include intermediate tool messages
            # These come from the model response tool_calls and tool results
            # recorded in the coordinator's session state. Since we don't
            # have direct access, we reconstruct from the final answer.
            pass

        self.history.append(
            Message(role="assistant", content=result.final_answer or "")
        )
        self.turn_count += 1

    def trim_to_token_budget(self, budget: int = MAX_HISTORY_TOKENS) -> None:
        """Keep only the most recent messages within the token budget.

        Uses rough estimate: ~4 chars/token for ASCII, ~1.5 chars/token for CJK.
        """
        if not self.history:
            return
        total = self._estimate_tokens()
        while total > budget and len(self.history) >= 2:
            # Remove oldest user+assistant pair
            self.history.pop(0)
            if self.history and self.history[0].role == "assistant":
                self.history.pop(0)
            total = self._estimate_tokens()

    def _estimate_tokens(self) -> int:
        total = 0
        for m in self.history:
            text = m.content or ""
            # CJK-aware: count CJK chars separately
            cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
            ascii_chars = len(text) - cjk
            total += ascii_chars // 4 + int(cjk / 1.5)
        return max(total, 1)

    def clear(self) -> None:
        self.history.clear()
        self.turn_count = 0

    def message_count(self) -> int:
        return len(self.history)


# ---------------------------------------------------------------------------
# Slash command registry
# ---------------------------------------------------------------------------

# handler signature: (framework, mock_model, repl_state, args_str) -> Awaitable[None]
_COMMANDS: dict[str, tuple[Any, str, str, str]] = {}


def _register_cmd(name: str, desc: str, usage: str = "", category: str = "通用") -> Any:
    """Decorator to register a slash command."""
    def decorator(func):
        _COMMANDS[name] = (func, desc, usage, category)
        return func
    return decorator


# ── 通用 ──────────────────────────────────────────────────────────────────

@_register_cmd("help", "显示所有可用命令", category="通用")
async def _cmd_help(fw, mock, state, args):
    categories: dict[str, list[str]] = {}
    for name, (_, desc, usage, cat) in sorted(_COMMANDS.items()):
        categories.setdefault(cat, []).append((name, desc, usage))
    print()
    for cat, cmds in categories.items():
        print(f"  {_bold(_yellow(f'[ {cat} ]'))}")
        for name, desc, usage in cmds:
            hint = f" {_dim(usage)}" if usage else ""
            print(f"    {_cyan('/' + name):24s} {desc}{hint}")
        print()
    print(f"  {_dim('直接输入文本与 Agent 对话。所有命令均以 / 开头。')}")


@_register_cmd("exit", "退出程序", category="通用")
async def _cmd_exit(fw, mock, state, args):
    pass  # handled in REPL loop


@_register_cmd("clear", "清屏", category="通用")
async def _cmd_clear(fw, mock, state, args):
    os.system("cls" if os.name == "nt" else "clear")


@_register_cmd("reset", "重置对话状态（清空历史 + Mock 状态）", category="通用")
async def _cmd_reset(fw, mock, state, args):
    state.clear()
    if mock:
        mock._reset_turn()
    print(f"  {_green('对话历史已清空')} ({_dim('Mock 状态也已重置' if mock else '在线模式')})")


# ── 查看 ──────────────────────────────────────────────────────────────────

@_register_cmd("tools", "列出所有已注册工具", category="查看")
async def _cmd_tools(fw, mock, state, args):
    tools = fw._registry.list_tools() if fw._registry else []
    # Group by category
    by_cat: dict[str, list] = {}
    for t in tools:
        cat = t.meta.category or "other"
        by_cat.setdefault(cat, []).append(t)
    print(f"\n  {_bold('已注册工具')} ({len(tools)}):\n")
    for cat, entries in sorted(by_cat.items()):
        print(f"  {_yellow(f'  [{cat}]')}")
        for t in entries:
            src = _dim(f"({t.meta.source})")
            confirm = _red("*") if t.meta.require_confirm else " "
            print(f"    {confirm} {_green(t.meta.name):24s} {src}  {_dim(t.meta.description[:50])}")
        print()
    if not tools:
        print(f"    {_dim('(无工具)')}")
    print(f"  {_dim(_red('*') + ' = 需要确认    使用 /call <tool> 直接调用工具')}")


@_register_cmd("skills", "列出所有已注册技能", category="查看")
async def _cmd_skills(fw, mock, state, args):
    skills = fw.list_skills()
    active = fw.get_active_skill()
    print(f"\n  {_bold('已注册技能')} ({len(skills)}):\n")
    for s in skills:
        kw = ", ".join(s.trigger_keywords) if s.trigger_keywords else "-"
        is_active = _yellow(" [ACTIVE]") if active and active.skill_id == s.skill_id else ""
        print(f"    {_magenta(s.skill_id):20s} {s.name}{is_active}")
        print(f"      触发词: {_dim(kw)}")
        if s.description:
            print(f"      描述:   {_dim(s.description[:70])}")
    if not skills:
        print(f"    {_dim('(无技能)')}")
    print(f"\n  {_dim('技能会根据输入关键词自动激活。也可用 /skill <id> 手动激活。')}")


@_register_cmd("memories", "查看已保存的记忆", category="查看")
async def _cmd_memories(fw, mock, state, args):
    mm = fw._deps.memory_manager if fw._deps else None
    if not mm:
        print(f"    {_dim('(记忆系统未初始化)')}")
        return
    agent_id = fw._agent.agent_id if fw._agent else ""
    records = mm.list_memories(agent_id, None)
    print(f"\n  {_bold('已保存记忆')} ({len(records)}):\n")
    for i, r in enumerate(records):
        kind = _cyan(f"[{r.kind.value}]")
        pin = _yellow(" [pinned]") if r.pinned else ""
        active = "" if r.active else _dim(" (inactive)")
        print(f"    {_dim(f'#{i}')} {kind} {r.title}{pin}{active}")
        if r.content:
            print(f"       {_dim(r.content[:80])}")
    if not records:
        print(f"    {_dim('(无记忆)')}")


@_register_cmd("config", "显示当前配置摘要", category="查看")
async def _cmd_config(fw, mock, state, args):
    cfg = fw.config
    print(f"\n  {_bold('当前配置:')}")
    rows = [
        ("适配器", cfg.model.adapter_type),
        ("模型", cfg.model.default_model_name),
        ("温度", str(cfg.model.temperature)),
        ("最大输出 tokens", str(cfg.model.max_output_tokens)),
        ("API Base", cfg.model.api_base or "(default)"),
        ("上下文窗口", str(cfg.context.max_context_tokens)),
        ("压缩策略", cfg.context.default_compression_strategy),
        ("记忆 DB", cfg.memory.db_path),
        ("自动提取记忆", str(cfg.memory.auto_extract_memory)),
        ("配置技能数", str(len(cfg.skills.definitions))),
    ]
    for label, val in rows:
        print(f"    {label:18s} {_cyan(val)}")


@_register_cmd("stats", "显示上下文统计信息", category="查看")
async def _cmd_stats(fw, mock, state, args):
    try:
        stats = fw._deps.context_engineer.report_context_stats()
        print(f"\n  {_bold('上下文统计:')}")
        print(f"    系统提示 tokens: {stats.system_tokens}")
        print(f"    记忆 tokens:     {stats.memory_tokens}")
        print(f"    会话历史 tokens: {stats.session_tokens}")
        print(f"    当前输入 tokens: {stats.input_tokens}")
        print(f"    总计 tokens:     {_cyan(str(stats.total_tokens))}")
        print(f"    裁剪组数:        {stats.groups_trimmed}")
    except Exception:
        print(f"    {_dim('(尚无统计数据，先发送一条消息)')}")


# ── 会话 ──────────────────────────────────────────────────────────────────

@_register_cmd("history", "查看对话历史", usage="/history [n]", category="会话")
async def _cmd_history(fw, mock, state, args):
    if not state.history:
        print(f"  {_dim('(对话历史为空)')}")
        return
    n = int(args) if args.strip().isdigit() else len(state.history)
    msgs = state.history[-n:]
    est_tokens = state._estimate_tokens()
    print(f"\n  {_bold('对话历史')} ({state.turn_count} 轮, {state.message_count()} 条消息, ~{est_tokens} tokens):\n")
    for i, m in enumerate(msgs):
        if m.role == "user":
            print(f"  {_cyan('User:')} {m.content[:120]}")
        elif m.role == "assistant":
            text = (m.content or "")[:120]
            print(f"  {_green('Agent:')} {text}")
            if i < len(msgs) - 1:
                print()
    print(f"\n  {_dim(f'Token 预算: {MAX_HISTORY_TOKENS}  |  当前: ~{est_tokens}')}")


@_register_cmd("history-clear", "清空对话历史", category="会话")
async def _cmd_history_clear(fw, mock, state, args):
    state.clear()
    print(f"  {_green('对话历史已清空')}")


# ── 工具 ──────────────────────────────────────────────────────────────────

@_register_cmd("call", "直接调用工具", usage="/call <tool_name> {json_args}", category="工具")
async def _cmd_call(fw, mock, state, args):
    """Parse: /call <tool_name> <json_args_or_positional>"""
    if not args:
        print(f"  {_red('用法:')} /call <tool_name> {{\"arg\": \"value\"}}")
        print(f"  {_dim('示例:')} /call calculator {{\"expression\": \"2+3\"}}")
        print(f"         /call weather {{\"city\": \"北京\"}}")
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

    # Parse arguments
    try:
        if raw_args.startswith("{"):
            call_args = json.loads(raw_args)
        elif raw_args:
            # Try to map positional args to parameter names
            sig = inspect.signature(entry.callable_ref)
            param_names = [p for p in sig.parameters if p != "self"]
            positional = raw_args.split(maxsplit=len(param_names) - 1)
            call_args = dict(zip(param_names, positional))
        else:
            call_args = {}
    except json.JSONDecodeError:
        # Treat entire string as first parameter
        sig = inspect.signature(entry.callable_ref)
        first_param = next(iter(sig.parameters), None)
        if first_param:
            call_args = {first_param: raw_args}
        else:
            call_args = {}

    print(f"  {_dim(f'调用: {tool_name}({call_args})')}")

    try:
        func = entry.callable_ref
        if inspect.iscoroutinefunction(func):
            result = await func(**call_args)
        else:
            result = func(**call_args)
        print(f"\n  {_green('结果:')}")
        if isinstance(result, (dict, list)):
            print(f"  {json.dumps(result, indent=2, ensure_ascii=False)}")
        else:
            print(f"  {result}")
    except Exception as e:
        print(f"  {_red('调用失败:')} {e}")


@_register_cmd("tool", "查看工具详细信息", usage="/tool <name>", category="工具")
async def _cmd_tool_detail(fw, mock, state, args):
    if not args:
        print(f"  {_red('用法:')} /tool <tool_name>")
        return
    name = args.strip()
    registry = fw._registry
    if not registry or not registry.has_tool(name):
        print(f"  {_red('工具不存在:')} {name}")
        return
    entry = registry.get_tool(name)
    m = entry.meta
    print(f"\n  {_bold(_green(m.name))}")
    print(f"    来源:     {m.source}")
    print(f"    分类:     {m.category or '-'}")
    print(f"    需确认:   {_red('是') if m.require_confirm else _green('否')}")
    print(f"    描述:     {m.description}")
    if m.tags:
        print(f"    标签:     {', '.join(m.tags)}")
    if entry.callable_ref:
        sig = inspect.signature(entry.callable_ref)
        print(f"    参数签名: {name}{sig}")
        for pname, param in sig.parameters.items():
            anno = param.annotation.__name__ if hasattr(param.annotation, '__name__') else str(param.annotation)
            default = f" = {param.default}" if param.default is not inspect.Parameter.empty else ""
            print(f"      {_cyan(pname):16s} {_dim(anno)}{default}")
    if entry.validator_model:
        print(f"    Schema:   {entry.validator_model.model_json_schema()}")


# ── 技能 ──────────────────────────────────────────────────────────────────

@_register_cmd("skill", "手动激活/查看技能", usage="/skill <id> | /skill off", category="技能")
async def _cmd_skill(fw, mock, state, args):
    if not args:
        active = fw.get_active_skill()
        if active:
            print(f"  当前活跃技能: {_magenta(active.skill_id)} ({active.name})")
            print(f"    Addon: {_dim(active.system_prompt_addon[:80])}")
        else:
            print(f"  {_dim('当前无活跃技能。使用 /skill <id> 激活。')}")
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
        print(f"  {_dim('可用: ' + ', '.join(s.skill_id for s in router.list_skills()))}")
        return

    fw.activate_skill(found)
    print(f"  {_green('已激活技能:')} {_magenta(found.skill_id)} ({found.name})")
    print(f"    Addon: {_dim(found.system_prompt_addon[:80])}")


@_register_cmd("skill-add", "动态注册新技能", usage='/skill-add <id> <keywords> "<addon>"', category="技能")
async def _cmd_skill_add(fw, mock, state, args):
    if not args:
        print(f"  {_red('用法:')} /skill-add <skill_id> <kw1,kw2,...> \"<system prompt addon>\"")
        print(f'  {_dim("示例:")} /skill-add poet 写诗,诗歌,poem "你是一位古诗词大师。请用优美的诗句回答。"')
        return

    parts = args.split(maxsplit=2)
    if len(parts) < 3:
        print(f"  {_red('参数不足。')} 需要: skill_id, keywords, addon_prompt")
        return

    sid = parts[0]
    keywords = [k.strip() for k in parts[1].split(",") if k.strip()]
    addon = parts[2].strip().strip('"').strip("'")

    skill = Skill(
        skill_id=sid,
        name=sid,
        trigger_keywords=keywords,
        system_prompt_addon=addon,
    )
    fw.register_skill(skill)
    print(f"  {_green('已注册技能:')} {_magenta(sid)}")
    print(f"    关键词: {', '.join(keywords)}")
    print(f"    Addon:  {_dim(addon[:60])}")


@_register_cmd("skill-rm", "移除一个技能", usage="/skill-rm <id>", category="技能")
async def _cmd_skill_rm(fw, mock, state, args):
    if not args:
        print(f"  {_red('用法:')} /skill-rm <skill_id>")
        return
    if fw.remove_skill(args.strip()):
        print(f"  {_green('已移除:')} {args.strip()}")
    else:
        print(f"  {_red('未找到:')} {args.strip()}")


# ── 记忆 ──────────────────────────────────────────────────────────────────

@_register_cmd("memory-clear", "清空所有记忆", category="记忆")
async def _cmd_memory_clear(fw, mock, state, args):
    mm = fw._deps.memory_manager if fw._deps else None
    if not mm:
        return
    count = fw.clear_memories()
    print(f"  {_green(f'已清除 {count} 条记忆')}")


@_register_cmd("memory-toggle", "开关记忆系统", usage="/memory-toggle on|off", category="记忆")
async def _cmd_memory_toggle(fw, mock, state, args):
    if args.strip().lower() in ("on", "true", "1"):
        fw.set_memory_enabled(True)
        print(f"  {_green('记忆系统: 已开启')}")
    elif args.strip().lower() in ("off", "false", "0"):
        fw.set_memory_enabled(False)
        print(f"  {_yellow('记忆系统: 已关闭')}")
    else:
        print(f"  {_red('用法:')} /memory-toggle on|off")


# ── 演示 ──────────────────────────────────────────────────────────────────

@_register_cmd("demo", "运行内置演示场景", usage="/demo [calc|weather|multi|skill]", category="演示")
async def _cmd_demo(fw, mock, state, args):
    demos = {
        "calc":    "帮我计算 42*13+7",
        "weather": "查询北京和上海的天气",
        "multi":   "算一下 100/3 然后查深圳天气",
        "skill":   "帮我用数学方法分析 2**10 的值",
        "note":    "帮我记录一条笔记: 今天学习了 Agent Framework",
    }
    if not args or args.strip() not in demos:
        print(f"\n  {_bold('可用演示场景:')}")
        for k, v in demos.items():
            print(f"    {_cyan('/demo ' + k):28s} {_dim(v)}")
        return

    task = demos[args.strip()]
    print(f"\n  {_dim(f'>>> 模拟输入: {task}')}")
    if mock:
        mock._reset_turn()
    try:
        result = await fw.run(task)
        _print_result(result)
    except Exception as e:
        print(f"  {_red('演示失败:')} {e}")


@_register_cmd("demo-all", "依次运行所有演示场景", category="演示")
async def _cmd_demo_all(fw, mock, state, args):
    demos = ["calc", "weather", "multi", "note", "skill"]
    for d in demos:
        print(f"\n  {_bold(_yellow(f'─── demo: {d} ───'))}")
        await _cmd_demo(fw, mock, state, d)
        print()


# ---------------------------------------------------------------------------
# Result printer
# ---------------------------------------------------------------------------

def _render_tool_output(output: Any, tool_name: str) -> str:
    """Render tool output for human-readable display."""
    if isinstance(output, dict):
        # Special handling for run_command: show stdout/stderr inline
        if tool_name == "run_command" and "stdout" in output:
            parts = []
            stdout = output.get("stdout", "").strip()
            stderr = output.get("stderr", "").strip()
            rc = output.get("return_code", 0)
            if stdout:
                parts.append(stdout)
            if stderr:
                parts.append(f"(stderr) {stderr}")
            if not parts:
                parts.append(f"(exit {rc}, no output)")
            return "\n".join(parts)
        return json.dumps(output, ensure_ascii=False, indent=2)
    if isinstance(output, list):
        return json.dumps(output, ensure_ascii=False, indent=2)
    return str(output) if output else "(no output)"


def _print_result(result: Any) -> None:
    if getattr(result, "iteration_history", None):
        print(f"\n  {_bold('执行轨迹:')}")
        for it in result.iteration_history:
            print(f"  {_dim(f'[Iteration {it.iteration_index + 1}]')}")
            resp = it.model_response
            if resp and resp.content:
                preview = resp.content if len(resp.content) <= 500 else resp.content[:500] + "\n... [truncated]"
                print(f"    {_cyan('主Agent输出:')}")
                for line in preview.splitlines():
                    print(f"      {line}")
            if resp and resp.tool_calls:
                tool_names = ", ".join(tc.function_name for tc in resp.tool_calls)
                print(f"    {_yellow('工具调用:')} {tool_names}")
            for tr in it.tool_results:
                print(f"    {_magenta(f'工具结果[{tr.tool_name}]')}:")
                if tr.success:
                    out = tr.output
                    rendered = _render_tool_output(out, tr.tool_name)
                    if len(rendered) > 1000:
                        rendered = rendered[:1000] + "\n... [truncated]"
                    for line in rendered.splitlines():
                        print(f"      {line}")
                else:
                    err_msg = str(tr.error) if tr.error else str(tr.output or "未知错误")
                    print(f"      {_red(err_msg)}")

    if result.success:
        print(f"\n  {_green('Agent 回复:')}")
        print(f"  {'─' * 56}")
        answer = result.final_answer or "(无回答)"
        for line in answer.split("\n"):
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


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

def _print_banner(use_mock: bool, config_path: str | None, fw: Any) -> None:
    print()
    print(f"  {_bold(_cyan('╔══════════════════════════════════════════════════════╗'))}")
    print(f"  {_bold(_cyan('║'))}{_bold('       AI Agent Framework — Interactive Terminal       ')}{_bold(_cyan('║'))}")
    print(f"  {_bold(_cyan('╚══════════════════════════════════════════════════════╝'))}")
    print()

    mode = _yellow("Mock (离线)") if use_mock else _green(f"在线: {fw.config.model.default_model_name}")
    print(f"  {_bold('模式')}  {mode}")
    if config_path:
        print(f"  {_bold('配置')}  {_dim(config_path)}")

    tool_count = len(fw._registry.list_tools()) if fw._registry else 0
    skill_count = len(fw.list_skills())
    print(f"  {_bold('工具')}  {_cyan(str(tool_count))} 个    {_bold('技能')}  {_magenta(str(skill_count))} 个")
    print()
    print(f"  {_dim('输入 /help 查看命令    直接输入文本与 Agent 对话')}")
    print(f"  {_dim('输入 /exit 或 Ctrl+C 退出')}")
    print()


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

async def _repl(fw: Any, mock_model: InteractiveMockModel | None) -> None:
    state = ReplState()

    while True:
        try:
            user_input = input(_rl_wrap(f"{_bold(_green('> '))}")).strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_dim('再见!')}")
            break

        if not user_input:
            continue

        # Slash command dispatch
        if user_input.startswith("/"):
            cmd_line = user_input[1:]
            parts = cmd_line.split(maxsplit=1)
            cmd_name = parts[0].lower() if parts else ""
            cmd_args = parts[1] if len(parts) > 1 else ""

            if cmd_name in ("exit", "quit", "q"):
                print(f"{_dim('再见!')}")
                break

            if cmd_name in _COMMANDS:
                handler, _, _, _ = _COMMANDS[cmd_name]
                await handler(fw, mock_model, state, cmd_args)
                continue
            else:
                suggestions = [n for n in _COMMANDS if n.startswith(cmd_name)]
                if suggestions:
                    print(f"  {_red(f'未知命令: /{cmd_name}')}")
                    print(f"  {_dim('你是否想要:')} {', '.join(_cyan('/' + s) for s in suggestions)}")
                else:
                    print(f"  {_red(f'未知命令: /{cmd_name}')}  {_dim('输入 /help 查看所有命令')}")
                continue

        # Agent conversation
        if mock_model:
            mock_model._reset_turn()

        try:
            result = await fw.run(
                user_input,
                initial_session_messages=state.history,
            )
            _print_result(result)
            if result.success:
                state.append_turn(user_input, result)
                state.trim_to_token_budget()
        except Exception as e:
            print(f"\n  {_red('运行错误:')}")
            print(f"  {e}")
            if os.environ.get("DEBUG"):
                traceback.print_exc()

    await fw.shutdown()


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
  python -m agent_framework.main                              # Mock (离线)
  python -m agent_framework.main -c config/doubao.json        # 豆包模型
  python -m agent_framework.main -c config/deepseek.json      # DeepSeek
  python -m agent_framework.main --mock                       # 强制 Mock
  DEBUG=1 python -m agent_framework.main                      # 详细错误
        """,
    )
    parser.add_argument("--config", "-c", help="配置文件路径 (JSON)")
    parser.add_argument("--mock", action="store_true", help="强制使用 Mock 模型")
    parser.add_argument("--no-approve", action="store_true", help="工具调用需要手动确认")
    args = parser.parse_args()

    use_mock = args.mock or (args.config is None)
    auto_approve = not args.no_approve

    try:
        fw, mock_model = _build_framework(
            config_path=args.config,
            use_mock=use_mock,
            auto_approve=auto_approve,
        )
    except Exception as e:
        print(f"{_red('框架初始化失败:')} {e}")
        if os.environ.get("DEBUG"):
            traceback.print_exc()
        sys.exit(1)

    _print_banner(mock_model is not None, args.config, fw)
    asyncio.run(_repl(fw, mock_model))


if __name__ == "__main__":
    main()

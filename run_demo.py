"""手动运行演示：完整的输入→推理→工具调用→输出 流程。

无需 API Key，使用 Mock 模型模拟 LLM 行为。
运行方式：python run_demo.py
"""

import asyncio
import json
import logging
from typing import AsyncIterator, Any

# 抑制 structlog JSON 日志输出，保持演示输出干净
logging.getLogger("agent_framework").setLevel(logging.WARNING)

from agent_framework.adapters.model.base_adapter import BaseModelAdapter, ModelChunk
from agent_framework.agent.coordinator import RunCoordinator
from agent_framework.agent.default_agent import DefaultAgent
from agent_framework.agent.react_agent import ReActAgent
from agent_framework.agent.runtime_deps import AgentRuntimeDeps
from agent_framework.agent.skill_router import SkillRouter
from agent_framework.context.builder import ContextBuilder
from agent_framework.context.compressor import ContextCompressor
from agent_framework.context.engineer import ContextEngineer
from agent_framework.context.source_provider import ContextSourceProvider
from agent_framework.memory.default_manager import DefaultMemoryManager
from agent_framework.memory.sqlite_store import SQLiteMemoryStore
from agent_framework.models.message import Message, ModelResponse, TokenUsage, ToolCallRequest
from agent_framework.tools.catalog import GlobalToolCatalog
from agent_framework.tools.confirmation import AutoApproveConfirmationHandler
from agent_framework.tools.decorator import tool
from agent_framework.tools.executor import ToolExecutor
from agent_framework.tools.registry import ToolRegistry


# ============================================================
# 1. 定义业务工具
# ============================================================

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


@tool(name="weather", description="查询城市天气", category="info")
def weather(city: str) -> str:
    """查询指定城市的天气信息。"""
    fake_data = {
        "北京": "晴天, 28°C, 湿度 40%",
        "上海": "多云, 25°C, 湿度 65%",
        "深圳": "阵雨, 30°C, 湿度 80%",
        "东京": "晴天, 22°C, 湿度 55%",
    }
    return fake_data.get(city, f"未找到 {city} 的天气数据")


@tool(name="note", description="保存一条笔记", category="util")
def note(title: str, content: str) -> str:
    """保存笔记。"""
    return f"已保存笔记 [{title}]: {content}"


# ============================================================
# 2. Mock 模型 —— 根据工具和上下文模拟 LLM 决策
# ============================================================

class SmartMockModel(BaseModelAdapter):
    """模拟 LLM 行为：识别用户意图 → 调用工具 → 整合结果。"""

    def __init__(self):
        self._call_count = 0
        self._scenario: str = ""
        self._tool_results: list[str] = []
        self._last_seen_msg_count: int = 0

    def set_scenario(self, scenario: str):
        self._scenario = scenario
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

        # 收集新的工具返回结果（只看上次之后的消息）
        new_messages = messages[self._last_seen_msg_count:]
        self._last_seen_msg_count = len(messages)
        for m in new_messages:
            if m.role == "tool" and m.content:
                self._tool_results.append(m.content)

        user_input = ""
        for m in messages:
            if m.role == "user":
                user_input = m.content or ""

        # 场景调度
        if self._scenario == "calc":
            return self._handle_calc(user_input)
        elif self._scenario == "weather":
            return self._handle_weather(user_input)
        elif self._scenario == "multi_tool":
            return self._handle_multi_tool(user_input)
        elif self._scenario == "react":
            return self._handle_react(user_input)
        elif self._scenario == "memory":
            return self._handle_memory(user_input)
        elif self._scenario == "spawn":
            return self._handle_spawn(user_input)
        elif self._scenario == "sub_task":
            return self._handle_sub_task(user_input)
        else:
            return ModelResponse(
                content=f"收到你的消息: {user_input}",
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
            )

    def _handle_calc(self, user_input: str) -> ModelResponse:
        if self._call_count == 1:
            return ModelResponse(
                content="让我帮你计算一下。",
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="calculator", arguments={"expression": "42 * 13 + 7"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=60, completion_tokens=30, total_tokens=90),
            )
        else:
            result = self._tool_results[-1] if self._tool_results else "无结果"
            return ModelResponse(
                content=f"计算完成！\n\n结果：{result}\n\n这是通过 calculator 工具计算得出的。",
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=80, completion_tokens=40, total_tokens=120),
            )

    def _handle_weather(self, user_input: str) -> ModelResponse:
        if self._call_count == 1:
            return ModelResponse(
                content="我来查询这些城市的天气。",
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="weather", arguments={"city": "北京"}),
                    ToolCallRequest(id="tc2", function_name="weather", arguments={"city": "上海"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=60, completion_tokens=40, total_tokens=100),
            )
        else:
            results = "\n".join(f"  - {r}" for r in self._tool_results)
            return ModelResponse(
                content=f"天气查询结果：\n{results}\n\n以上是最新天气信息。",
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            )

    def _handle_multi_tool(self, user_input: str) -> ModelResponse:
        if self._call_count == 1:
            return ModelResponse(
                content="好的，我来帮你完成多项任务。",
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="calculator", arguments={"expression": "100 / 3"}),
                    ToolCallRequest(id="tc2", function_name="weather", arguments={"city": "深圳"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=70, completion_tokens=35, total_tokens=105),
            )
        elif self._call_count == 2:
            return ModelResponse(
                content="计算和天气都查到了，我再帮你记个笔记。",
                tool_calls=[
                    ToolCallRequest(id="tc3", function_name="note", arguments={
                        "title": "今日摘要",
                        "content": f"计算结果: {self._tool_results[0] if self._tool_results else ''}，深圳天气: {self._tool_results[1] if len(self._tool_results) > 1 else ''}"
                    }),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=120, completion_tokens=50, total_tokens=170),
            )
        else:
            return ModelResponse(
                content=f"全部任务完成！\n\n汇总：\n" + "\n".join(f"  ✓ {r}" for r in self._tool_results),
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=150, completion_tokens=60, total_tokens=210),
            )

    def _handle_react(self, user_input: str) -> ModelResponse:
        if self._call_count == 1:
            return ModelResponse(
                content="Thought: 用户想知道北京天气和一个计算结果，我先查天气。",
                tool_calls=[
                    ToolCallRequest(id="tc1", function_name="weather", arguments={"city": "北京"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=80, completion_tokens=40, total_tokens=120),
            )
        elif self._call_count == 2:
            return ModelResponse(
                content="Thought: 天气查到了，现在计算 2^10。",
                tool_calls=[
                    ToolCallRequest(id="tc2", function_name="calculator", arguments={"expression": "2**10"}),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=35, total_tokens=135),
            )
        else:
            weather_r = self._tool_results[0] if self._tool_results else ""
            calc_r = self._tool_results[1] if len(self._tool_results) > 1 else ""
            return ModelResponse(
                content=f"Thought: 两个信息都拿到了，可以回答了。\n\nFinal Answer: 北京天气 {weather_r}；2的10次方 {calc_r}。",
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=130, completion_tokens=50, total_tokens=180),
            )

    def _handle_spawn(self, user_input: str) -> ModelResponse:
        """Parent agent: spawn a sub-agent to handle sub-task."""
        if self._call_count == 1:
            return ModelResponse(
                content="我需要派生一个子 Agent 来分析数据。",
                tool_calls=[
                    ToolCallRequest(
                        id="tc_spawn",
                        function_name="spawn_agent",
                        arguments={
                            "task_input": "请分析以下数据并给出摘要：销售额增长15%，成本下降3%，利润率提升至22%。",
                            "mode": "ephemeral",
                            "memory_scope": "isolated",
                        },
                    ),
                ],
                finish_reason="tool_calls",
                usage=TokenUsage(prompt_tokens=80, completion_tokens=40, total_tokens=120),
            )
        else:
            result = self._tool_results[-1] if self._tool_results else "子Agent无结果"
            return ModelResponse(
                content=f"子 Agent 已完成分析。\n\n汇总结果：{result}",
                tool_calls=[], finish_reason="stop",
                usage=TokenUsage(prompt_tokens=100, completion_tokens=60, total_tokens=160),
            )

    def _handle_sub_task(self, user_input: str) -> ModelResponse:
        """Sub-agent: directly answer the delegated task."""
        return ModelResponse(
            content="数据分析摘要：销售额同比增长15%，成本优化效果显著（下降3%），综合利润率达22%，整体经营状况良好，建议继续当前策略。",
            tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=40, completion_tokens=30, total_tokens=70),
        )

    def _handle_memory(self, user_input: str) -> ModelResponse:
        return ModelResponse(
            content="好的，我已经记住你的偏好了。以后我会用中文回答你的所有问题。",
            tool_calls=[], finish_reason="stop",
            usage=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
        )

    async def stream_complete(self, messages, tools=None) -> AsyncIterator[ModelChunk]:
        resp = await self.complete(messages, tools)
        yield ModelChunk(delta_content=resp.content, finish_reason=resp.finish_reason)

    def count_tokens(self, messages: list[Message]) -> int:
        return sum(len(m.content or "") // 4 for m in messages)

    def supports_parallel_tool_calls(self) -> bool:
        return True


# ============================================================
# 3. 构建框架
# ============================================================

def build_framework(model: SmartMockModel):
    from agent_framework.tools.delegation import DelegationExecutor
    from agent_framework.subagent.runtime import SubAgentRuntime

    store = SQLiteMemoryStore(db_path=":memory:")
    memory = DefaultMemoryManager(store=store, auto_extract=True)

    catalog = GlobalToolCatalog()
    catalog.register_function(calculator)
    catalog.register_function(weather)
    catalog.register_function(note)

    # Register spawn_agent tool
    from agent_framework.tools.builtin.spawn_agent import spawn_agent
    catalog.register_function(spawn_agent)

    registry = ToolRegistry()
    for entry in catalog.list_all():
        registry.register(entry)

    delegation_executor = DelegationExecutor(sub_agent_runtime=None)

    executor = ToolExecutor(
        registry=registry,
        confirmation_handler=AutoApproveConfirmationHandler(),
        delegation_executor=delegation_executor,
    )
    engineer = ContextEngineer(
        source_provider=ContextSourceProvider(),
        builder=ContextBuilder(),
        compressor=ContextCompressor(),
    )

    deps = AgentRuntimeDeps(
        tool_registry=registry,
        tool_executor=executor,
        memory_manager=memory,
        context_engineer=engineer,
        model_adapter=model,
        skill_router=SkillRouter(),
        confirmation_handler=AutoApproveConfirmationHandler(),
        delegation_executor=delegation_executor,
    )

    # Wire SubAgentRuntime (after deps created so factory can use them)
    sub_runtime = SubAgentRuntime(
        parent_deps=deps,
        max_concurrent=3,
        max_per_run=5,
    )
    deps.sub_agent_runtime = sub_runtime
    delegation_executor._sub_agent_runtime = sub_runtime

    return deps, memory, store


# ============================================================
# 4. 演示场景
# ============================================================

def print_header(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(result, show_details=True):
    status = "成功 ✓" if result.success else "失败 ✗"
    print(f"\n  状态: {status}")
    print(f"  迭代次数: {result.iterations_used}")
    print(f"  Token 用量: {result.usage.total_tokens}")
    if result.stop_signal:
        print(f"  停止原因: {result.stop_signal.reason.value}")
    print(f"\n  --- Agent 回答 ---")
    print(f"  {result.final_answer or '(无回答)'}")
    if result.error:
        print(f"\n  错误: {result.error}")


async def demo_1_simple(model, deps):
    """演示 1：简单对话（无工具调用）"""
    print_header("演示 1：简单对话")
    print("  用户输入: 你好，请介绍一下自己")

    model.set_scenario("simple")
    agent = DefaultAgent(agent_id="demo", system_prompt="你是一个中文助手。", model_name="mock")
    result = await RunCoordinator().run(agent, deps, "你好，请介绍一下自己")
    print_result(result)


async def demo_2_tool_call(model, deps):
    """演示 2：单工具调用（计算器）"""
    print_header("演示 2：工具调用 - 计算器")
    print("  用户输入: 帮我算一下 42 * 13 + 7")

    model.set_scenario("calc")
    agent = DefaultAgent(agent_id="demo", system_prompt="你是一个数学助手。", model_name="mock")
    result = await RunCoordinator().run(agent, deps, "帮我算一下 42 * 13 + 7")
    print_result(result)


async def demo_3_parallel_tools(model, deps):
    """演示 3：并行工具调用（同时查两个城市天气）"""
    print_header("演示 3：并行工具调用 - 多城市天气")
    print("  用户输入: 北京和上海今天天气怎么样？")

    model.set_scenario("weather")
    agent = DefaultAgent(agent_id="demo", system_prompt="你是一个天气助手。", model_name="mock")
    result = await RunCoordinator().run(agent, deps, "北京和上海今天天气怎么样？")
    print_result(result)


async def demo_4_multi_step(model, deps):
    """演示 4：多步工具链（计算→天气→笔记）"""
    print_header("演示 4：多步工具链 - 计算+天气+笔记")
    print("  用户输入: 算一下 100/3，查深圳天气，然后帮我记个笔记")

    model.set_scenario("multi_tool")
    agent = DefaultAgent(agent_id="demo", system_prompt="你是一个全能助手。", model_name="mock", max_iterations=10)
    result = await RunCoordinator().run(agent, deps, "算一下 100/3，查深圳天气，然后帮我记个笔记")
    print_result(result)


async def demo_5_react(model, deps):
    """演示 5：ReAct Agent（推理链 + 工具调用 + Final Answer 检测）"""
    print_header("演示 5：ReAct Agent - 推理+行动+观察")
    print("  用户输入: 北京天气如何？2的10次方是多少？")

    model.set_scenario("react")
    agent = ReActAgent(agent_id="react_demo", model_name="mock", max_iterations=10)
    result = await RunCoordinator().run(agent, deps, "北京天气如何？2的10次方是多少？")
    print_result(result)


async def demo_6_memory(model, deps, memory, store):
    """演示 6：记忆提取与持久化"""
    print_header("演示 6：Saved Memory - 自动提取用户偏好")
    print("  用户输入: 以后都用中文回答我")

    model.set_scenario("memory")
    agent = DefaultAgent(agent_id="demo_mem", system_prompt="你是一个助手。", model_name="mock")
    result = await RunCoordinator().run(agent, deps, "以后都用中文回答我")
    print_result(result)

    records = store.list_by_user("demo_mem", None, active_only=False)
    print(f"\n  --- 已保存的记忆 ({len(records)} 条) ---")
    for r in records:
        print(f"  [{r.kind.value}] {r.title}")
        print(f"    内容: {r.content}")
        print(f"    标签: {r.tags}")


async def demo_7_max_iterations(model, deps):
    """演示 7：最大迭代限制"""
    print_header("演示 7：最大迭代限制 (max_iterations=2)")
    print("  用户输入: 一个需要多步的任务")

    model.set_scenario("multi_tool")
    agent = DefaultAgent(agent_id="demo", system_prompt="你是助手。", model_name="mock", max_iterations=2)
    result = await RunCoordinator().run(agent, deps, "做一个需要很多步骤的复杂任务")
    print_result(result)


async def demo_9_subagent(model, deps):
    """演示 9：子 Agent 派生 — 父 Agent 委派任务给子 Agent"""
    print_header("演示 9：多智能体协调 - 子Agent派生")
    print("  用户输入: 帮我分析一下这些经营数据")
    print("  流程: 父Agent → spawn_agent → 子Agent执行 → 结果返回父Agent")

    # Parent agent uses spawn scenario
    model.set_scenario("spawn")

    # For the sub-agent, we need it to use "sub_task" scenario.
    # The SubAgentRuntime creates a child that runs through RunCoordinator,
    # which calls model.complete(). We switch scenario when the sub-agent runs.
    original_complete = model.complete

    spawn_call_detected = False

    async def patched_complete(messages, tools=None, temperature=None, max_tokens=None):
        nonlocal spawn_call_detected
        # Detect if this is a sub-agent call (system prompt contains "sub-agent")
        sys_msg = messages[0].content if messages and messages[0].role == "system" else ""
        if "sub-agent" in sys_msg.lower():
            old_scenario = model._scenario
            model.set_scenario("sub_task")
            result = await original_complete(messages, tools, temperature, max_tokens)
            model._scenario = old_scenario
            return result
        return await original_complete(messages, tools, temperature, max_tokens)

    model.complete = patched_complete

    agent = DefaultAgent(
        agent_id="parent",
        system_prompt="你是一个项目经理Agent，可以派生子Agent处理分析任务。",
        model_name="mock",
        allow_spawn_children=True,
    )
    result = await RunCoordinator().run(agent, deps, "帮我分析一下这些经营数据")
    print_result(result)

    # Restore original
    model.complete = original_complete


async def demo_10_spawn_denied(model, deps):
    """演示 10：子 Agent 递归防护 — 子Agent尝试再次派生被拒绝"""
    print_header("演示 10：子Agent递归防护 (PERMISSION_DENIED)")
    print("  场景: 子Agent(allow_spawn_children=False)尝试调用spawn_agent")

    from agent_framework.tools.delegation import DelegationExecutor
    from agent_framework.models.subagent import SubAgentSpec

    delegation = deps.delegation_executor
    # Simulate a sub-agent (allow_spawn_children=False) trying to spawn
    sub_agent = DefaultAgent(
        agent_id="sub_agent_test",
        system_prompt="子Agent",
        model_name="mock",
        allow_spawn_children=False,  # Factory forces this
    )

    spec = SubAgentSpec(task_input="尝试递归派生")
    result = await delegation.delegate_to_subagent(spec, sub_agent)

    status = "被拒绝 ✓" if not result.success and "PERMISSION_DENIED" in (result.error or "") else "未拦截 ✗"
    print(f"\n  递归防护: {status}")
    print(f"  错误信息: {result.error}")


async def demo_8_context_stats(model, deps):
    """演示 8：上下文统计"""
    print_header("演示 8：上下文工程 - 统计信息")
    print("  用户输入: 简单问题")

    model.set_scenario("simple")
    agent = DefaultAgent(agent_id="demo", system_prompt="你是助手。", model_name="mock")
    await RunCoordinator().run(agent, deps, "你好")

    stats = deps.context_engineer.report_context_stats()
    print(f"\n  --- Context Stats ---")
    print(f"  系统提示 tokens: {stats.system_tokens}")
    print(f"  记忆 tokens: {stats.memory_tokens}")
    print(f"  会话历史 tokens: {stats.session_tokens}")
    print(f"  当前输入 tokens: {stats.input_tokens}")
    print(f"  总计 tokens: {stats.total_tokens}")
    print(f"  裁剪组数: {stats.groups_trimmed}")


# ============================================================
# 5. 主入口
# ============================================================

async def main():
    print("=" * 60)
    print("  AI Agent Framework — 功能演示")
    print("  (使用 Mock 模型，无需 API Key)")
    print("=" * 60)

    model = SmartMockModel()
    deps, memory, store = build_framework(model)

    await demo_1_simple(model, deps)
    await demo_2_tool_call(model, deps)
    await demo_3_parallel_tools(model, deps)
    await demo_4_multi_step(model, deps)
    await demo_5_react(model, deps)
    await demo_6_memory(model, deps, memory, store)
    await demo_7_max_iterations(model, deps)
    await demo_8_context_stats(model, deps)
    await demo_9_subagent(model, deps)
    await demo_10_spawn_denied(model, deps)

    print(f"\n{'='*60}")
    print("  全部演示完成！")
    print(f"{'='*60}")

    # 显示注册的工具列表
    print(f"\n  已注册工具:")
    for entry in deps.tool_registry.list_tools():
        print(f"    - {entry.meta.name} ({entry.meta.source}): {entry.meta.description}")

    print()


if __name__ == "__main__":
    asyncio.run(main())

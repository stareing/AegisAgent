# Agent Framework 完整使用教程

## 目录

1. [安装](#1-安装)
2. [快速开始](#2-快速开始)
3. [配置系统](#3-配置系统)
4. [模型适配器](#4-模型适配器)
5. [自定义工具](#5-自定义工具)
6. [记忆系统](#6-记忆系统)
7. [上下文管理](#7-上下文管理)
8. [Skill 技能路由](#8-skill-技能路由)
9. [子Agent (SubAgent)](#9-子agent-subagent)
10. [ReAct 推理Agent](#10-react-推理agent)
11. [MCP 工具集成](#11-mcp-工具集成)
12. [A2A 多Agent协作](#12-a2a-多agent协作)
13. [CLI 命令行](#13-cli-命令行)
14. [编程接口详解](#14-编程接口详解)
15. [自定义 Agent](#15-自定义-agent)
16. [架构概览](#16-架构概览)
17. [环境变量](#17-环境变量)
18. [常见问题](#18-常见问题)

---

## 1. 安装

### 基础安装

```bash
pip install -e .
```

### 按需安装 SDK 依赖

```bash
# 单个模型 SDK
pip install -e ".[openai]"       # OpenAI + DeepSeek/豆包/Qwen/Zhipu/MiniMax/Custom
pip install -e ".[anthropic]"    # Anthropic Claude
pip install -e ".[google]"       # Google Gemini

# MCP / A2A 协议
pip install -e ".[mcp]"          # MCP 工具服务器
pip install -e ".[a2a]"          # A2A 多Agent协作

# 全部安装
pip install -e ".[all]"

# 开发环境（含测试）
pip install -e ".[dev]"
```

### 验证安装

```bash
python -c "from agent_framework.entry import AgentFramework; print('OK')"
```

---

## 2. 快速开始

### 2.1 最简示例（代码）

```python
import asyncio
from agent_framework.entry import AgentFramework

async def main():
    framework = AgentFramework(config_path="config/deepseek.json")
    framework.setup(auto_approve_tools=True)

    result = await framework.run("你好，请自我介绍")

    if result.success:
        print(result.final_answer)
    else:
        print(f"失败: {result.error}")

    await framework.shutdown()

asyncio.run(main())
```

### 2.2 命令行交互

```bash
# 交互式 REPL
agent-cli --config config/deepseek.json

# 单次执行
agent-cli --config config/qwen.json --task "解释什么是快速排序"

# 指定模型
agent-cli --config config/openai.json --model gpt-4o
```

### 2.3 运行内置演示（无需 API Key）

```bash
python run_demo.py
```

演示包括：工具调用、并行工具、ReAct 推理、记忆保存、子Agent 派生等 10 个场景。

---

## 3. 配置系统

### 3.1 配置文件

框架使用 JSON 配置文件，提供预设模板：

```
config.json              # 默认配置（全部字段）
config/
├── openai.json          # OpenAI GPT-4o
├── anthropic.json       # Claude Sonnet 4
├── google.json          # Gemini 2.0 Flash
├── deepseek.json        # DeepSeek
├── doubao.json          # 豆包（字节跳动）
├── qwen.json            # 通义千问（阿里巴巴）
├── zhipu.json           # 智谱 GLM-4
├── minimax.json         # MiniMax
├── custom.json          # 自定义端点模板
└── full.json            # 完整配置（含 MCP + A2A）
```

### 3.2 配置结构

```json
{
  "model": {
    "adapter_type": "deepseek",
    "default_model_name": "deepseek-chat",
    "temperature": 0.7,
    "max_output_tokens": 4096,
    "api_key": null,
    "api_base": null,
    "timeout_ms": 30000,
    "max_retries": 3
  },
  "context": {
    "max_context_tokens": 65536,
    "reserve_for_output": 4096,
    "compress_threshold_ratio": 0.85,
    "default_compression_strategy": "SLIDING_WINDOW",
    "spawn_seed_ratio": 0.3
  },
  "memory": {
    "db_path": "data/memories.db",
    "enable_saved_memory": true,
    "auto_extract_memory": true,
    "max_memories_in_context": 10,
    "max_memory_items_per_user": 200
  },
  "tools": {
    "confirmation_handler_type": "cli",
    "max_concurrent_tool_calls": 5,
    "allow_parallel_tool_calls": true
  },
  "subagent": {
    "max_sub_agents_per_run": 5,
    "max_concurrent_sub_agents": 3,
    "default_deadline_ms": 60000,
    "default_max_iterations": 10
  },
  "logging": {
    "log_dir": "logs",
    "json_output": true,
    "level": "INFO"
  }
}
```

### 3.3 环境变量覆盖

配置支持通过环境变量覆盖（前缀 `AGENT_`，嵌套用 `__` 分隔）：

```bash
export AGENT_MODEL__ADAPTER_TYPE=deepseek
export AGENT_MODEL__API_KEY=sk-xxx
export AGENT_MODEL__DEFAULT_MODEL_NAME=deepseek-chat
export AGENT_LOGGING__LEVEL=DEBUG
```

### 3.4 代码中动态配置

```python
from agent_framework.infra.config import FrameworkConfig, ModelConfig

config = FrameworkConfig(
    model=ModelConfig(
        adapter_type="qwen",
        default_model_name="qwen-max",
        api_key="sk-xxx",
        temperature=0.3,
    )
)
framework = AgentFramework(config=config)
```

---

## 4. 模型适配器

### 4.1 支持的模型

| adapter_type | 厂商 | 默认模型 | 备注 |
|---|---|---|---|
| `litellm` | 通用（默认） | `gpt-3.5-turbo` | 支持 100+ 模型，通过 litellm 路由 |
| `openai` | OpenAI | `gpt-4o` | 官方 openai SDK |
| `anthropic` | Anthropic | `claude-sonnet-4-20250514` | 官方 anthropic SDK |
| `google` | Google | `gemini-2.0-flash` | 官方 google-genai SDK |
| `deepseek` | DeepSeek | `deepseek-chat` | OpenAI 兼容 |
| `doubao` | 豆包/字节跳动 | `doubao-pro-32k` | Volcengine Ark 平台 |
| `qwen` | 通义千问/阿里巴巴 | `qwen-plus` | DashScope OpenAI 兼容模式 |
| `zhipu` | 智谱 | `glm-4` | OpenAI 兼容 |
| `minimax` | MiniMax | `abab6.5s-chat` | OpenAI 兼容 |
| `custom` | 自定义 | 用户指定 | 任意 OpenAI 兼容端点 |

### 4.2 各厂商配置示例

**DeepSeek：**
```bash
export DEEPSEEK_API_KEY=sk-xxx
agent-cli --config config/deepseek.json
```

**豆包：**
```bash
# 豆包使用 endpoint ID 作为模型名
export VOLCENGINE_API_KEY=xxx
agent-cli --config config/doubao.json --model ep-20240101-xxxxx
```

**通义千问：**
```bash
export DASHSCOPE_API_KEY=sk-xxx
agent-cli --config config/qwen.json --model qwen-max
```

**智谱 GLM：**
```bash
export ZHIPU_API_KEY=xxx.xxx
agent-cli --config config/zhipu.json --model glm-4-plus
```

**自定义端点（如本地 Ollama、vLLM）：**
```json
{
  "model": {
    "adapter_type": "custom",
    "default_model_name": "llama3",
    "api_key": "not-needed",
    "api_base": "http://localhost:11434/v1"
  }
}
```

### 4.3 代码中直接使用适配器

```python
from agent_framework.adapters.model.openai_compatible_adapter import DeepSeekAdapter

adapter = DeepSeekAdapter(
    model_name="deepseek-coder",
    api_key="sk-xxx",
)

# 或使用 LiteLLM 统一路由
from agent_framework.adapters.model.litellm_adapter import LiteLLMAdapter

adapter = LiteLLMAdapter(
    model_name="deepseek/deepseek-chat",  # litellm 格式
    api_base="https://api.deepseek.com",
)
```

---

## 5. 自定义工具

### 5.1 定义工具

使用 `@tool` 装饰器：

```python
from agent_framework.tools.decorator import tool

@tool(name="search", description="搜索互联网", category="search")
def web_search(query: str, max_results: int = 5) -> str:
    """搜索互联网并返回结果。"""
    # 你的搜索逻辑
    return f"搜索 '{query}' 的前 {max_results} 条结果..."

# 异步工具自动检测
@tool(name="fetch_url", description="获取网页内容", category="network")
async def fetch_url(url: str) -> str:
    """获取指定 URL 的内容。"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            return await resp.text()

# 需要用户确认的工具
@tool(name="delete_file", description="删除文件", category="filesystem", require_confirm=True)
def delete_file(path: str) -> str:
    """删除指定路径的文件。"""
    import os
    os.remove(path)
    return f"已删除: {path}"
```

### 5.2 注册工具

```python
# 方式 1：通过 framework 注册
framework.register_tool(web_search)

# 方式 2：注册整个模块
import my_tools
framework.register_tools_from_module(my_tools)

# 方式 3：通过 GlobalToolCatalog 手动注册
from agent_framework.tools.catalog import GlobalToolCatalog

catalog = GlobalToolCatalog()
catalog.register_function(web_search)
```

### 5.3 工具参数验证

参数类型通过函数签名自动推导，生成 pydantic 验证模型和 JSON Schema：

```python
@tool(name="create_task")
def create_task(
    title: str,                    # 必填字符串
    priority: int = 3,             # 可选整数，默认 3
    tags: list[str] | None = None, # 可选列表
) -> str:
    """创建一个任务。"""
    return f"任务已创建: {title}"
```

模型调用时收到的 JSON Schema：
```json
{
  "type": "function",
  "function": {
    "name": "create_task",
    "description": "创建一个任务。",
    "parameters": {
      "properties": {
        "title": {"type": "string"},
        "priority": {"type": "integer", "default": 3},
        "tags": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}
      },
      "required": ["title"]
    }
  }
}
```

### 5.4 工具确认策略

```python
# 方式 1：全部自动批准
framework.setup(auto_approve_tools=True)

# 方式 2：CLI 交互确认（默认）
framework.setup(auto_approve_tools=False)

# 方式 3：逐工具控制
@tool(require_confirm=True)    # 该工具执行前需用户确认
def dangerous_operation(): ...

@tool(require_confirm=False)   # 该工具自动执行
def safe_operation(): ...
```

---

## 6. 记忆系统

### 6.1 自动记忆提取

框架自动从对话中检测并保存用户偏好、约束和项目上下文：

```
用户: "以后用 Python 回答所有代码问题"
→ 自动保存为 USER_PREFERENCE 类型记忆

用户: "禁止使用 eval"
→ 自动保存为 USER_CONSTRAINT 类型记忆

用户: "我们正在开发一个 AI 框架"
→ 自动保存为 PROJECT_CONTEXT 类型记忆
```

### 6.2 记忆管理 API

```python
# 列出记忆
memories = framework.list_memories()
for m in memories:
    print(f"[{m.kind.value}] {m.title}: {m.content}")

# 删除记忆
framework.forget_memory("memory-id-xxx")

# 固定记忆（不被自动覆盖）
framework.pin_memory("memory-id-xxx")
framework.unpin_memory("memory-id-xxx")

# 启用/禁用记忆
framework.activate_memory("memory-id-xxx")
framework.deactivate_memory("memory-id-xxx")

# 全局开关
framework.set_memory_enabled(False)  # 禁用记忆系统

# 清空所有记忆
framework.clear_memories()
```

### 6.3 记忆选择策略

上下文注入时的优先级：
1. 固定 (pinned) 记忆优先
2. 关键词匹配的记忆
3. 最近更新的活跃记忆
4. 总数限制为 `max_memories_in_context`（默认 10）

### 6.4 记忆类型

| 类型 | 说明 | 自动检测 |
|---|---|---|
| `USER_PROFILE` | 用户基本信息 | 否 |
| `USER_PREFERENCE` | 偏好设置 | 是（"以后用…"、"always…"） |
| `USER_CONSTRAINT` | 约束限制 | 是（"禁止…"、"must not…"） |
| `PROJECT_CONTEXT` | 项目背景 | 是（"项目是…"、"正在开发…"） |
| `TASK_HINT` | 任务提示 | 否 |
| `CUSTOM` | 自定义 | 否 |

---

## 7. 上下文管理

### 7.1 五槽位上下文结构

每次 LLM 调用的上下文按以下顺序组装：

```
┌─────────────────────────────────┐
│  Slot 1: System Core            │  系统提示词
│  Slot 2: Skill Addon            │  技能附加提示（可选）
│  Slot 3: Saved Memories         │  保存的记忆
├─────────────────────────────────┤
│  Slot 4: Session History        │  会话历史（可裁剪）
├─────────────────────────────────┤
│  Slot 5: Current Input          │  当前用户输入
└─────────────────────────────────┘
```

### 7.2 上下文压缩策略

当会话历史超出 token 预算时，自动应用压缩：

| 策略 | 说明 |
|---|---|
| `SLIDING_WINDOW` | 保留最近的消息组，丢弃最旧的（默认） |
| `TOOL_RESULT_SUMMARY` | 截断过长的工具返回结果 |
| `LLM_SUMMARIZE` | 用 LLM 总结早期历史（规划中） |

### 7.3 事务组 (Transaction Group)

工具调用消息不会被拆分：

```
[assistant + tool_calls]  ← 这些消息作为一个原子组
[tool result 1]              不会被单独丢弃
[tool result 2]
```

---

## 8. Skill 技能路由

### 8.1 定义技能

```python
from agent_framework.models.agent import Skill

math_skill = Skill(
    skill_id="math",
    name="数学计算",
    description="专注于数学问题求解",
    trigger_keywords=["计算", "算一下", "calculate"],
    system_prompt_addon="你是一个数学专家。使用工具进行精确计算，不要心算。",
    model_override="gpt-4o",          # 可选：该技能使用更强模型
    temperature_override=0.1,          # 可选：降低随机性
)
```

### 8.2 注册和使用

```python
framework.setup()
framework._deps.skill_router.register_skill(math_skill)

# 用户输入包含关键词时自动激活
result = await framework.run("计算 2^10 + 3^5")
# → 自动匹配 math_skill，注入数学专家提示
```

### 8.3 技能生命周期

1. 用户输入到达 → SkillRouter 检测关键词
2. 匹配到技能 → 注入 `system_prompt_addon` 到上下文
3. 可选覆盖模型和温度
4. 运行结束 → 自动清理技能上下文（包括异常路径）

---

## 9. 子Agent (SubAgent)

### 9.1 概念

主 Agent 可以通过 `spawn_agent` 工具派生子 Agent 来处理子任务。子 Agent：
- 独立运行，有自己的迭代循环
- 受限工具集（默认排除 system/network/subagent 类别）
- 不可递归派生（强制 `allow_spawn_children=False`）
- 有并发和配额限制

### 9.2 代码中使用

```python
# 主 Agent 需要开启派生权限
agent = DefaultAgent(allow_spawn_children=True)
framework.setup(agent=agent, auto_approve_tools=True)

# 模型在推理过程中会自动调用 spawn_agent 工具
result = await framework.run("分析这段代码并写单元测试")
```

### 9.3 内存域 (Memory Scope)

| 域 | 读取 | 写入 | 说明 |
|---|---|---|---|
| `ISOLATED` | 仅自身 | 仅自身 | 完全隔离（默认） |
| `INHERIT_READ` | 自身 + 父快照 | 仅自身 | 可读取父记忆冻结快照 |
| `SHARED_WRITE` | 父快照 | 写入父记忆 | 可读父快照，写入共享到父 |

快照语义：子 Agent 读到的是 **spawn 时刻**的父记忆冻结副本，运行期间不感知父记忆变化。

### 9.4 配额控制

```json
{
  "subagent": {
    "max_sub_agents_per_run": 5,
    "max_concurrent_sub_agents": 3,
    "default_deadline_ms": 60000,
    "default_max_iterations": 10
  }
}
```

---

## 10. ReAct 推理Agent

### 10.1 什么是 ReAct

ReAct (Reasoning + Acting) 模式让 Agent 交替进行推理和工具调用：

```
Thought: 我需要查询天气
Action: weather(city="北京")
Observation: 晴天, 28°C
Thought: 现在我有了天气信息
Final Answer: 北京今天晴天，28°C。
```

### 10.2 使用 ReActAgent

```python
from agent_framework.agent.react_agent import ReActAgent

agent = ReActAgent(
    system_prompt="你是一个研究助手",
    model_name="gpt-4o",
    max_iterations=25,
    max_react_steps=10,     # ReAct 步数上限（可选）
    temperature=0.2,
)

framework.setup(agent=agent)
result = await framework.run("研究 Python 3.12 的新特性")
```

### 10.3 ReAct 特性

- 自动注入 ReAct 协议提示词
- 检测 `Final Answer:` 模式作为停止条件
- 错误策略：迭代未用完时 RETRY，最后一次 ABORT
- `extract_final_answer()` 静态方法可提取最终答案

---

## 11. MCP 工具集成

### 11.1 配置 MCP 服务器

```json
{
  "mcp": {
    "servers": [
      {
        "server_id": "filesystem",
        "transport": "stdio",
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
      },
      {
        "server_id": "database",
        "transport": "sse",
        "url": "http://localhost:8080/sse"
      }
    ]
  }
}
```

### 11.2 代码中连接

```python
framework.setup()
await framework.setup_mcp()

# MCP 服务器的工具自动注册，命名为 mcp::<server_id>::<tool_name>
# 例如: mcp::filesystem::read_file, mcp::database::query
```

### 11.3 支持的传输协议

| 协议 | 配置 | 说明 |
|---|---|---|
| `stdio` | `command` + `args` | 启动子进程通信 |
| `sse` | `url` | Server-Sent Events |
| `streamable_http` | `url` | Streamable HTTP |

---

## 12. A2A 多Agent协作

### 12.1 配置已知Agent

```json
{
  "a2a": {
    "known_agents": [
      {"url": "http://localhost:9000", "alias": "code-reviewer"},
      {"url": "http://localhost:9001", "alias": "translator"}
    ]
  }
}
```

### 12.2 代码中连接

```python
framework.setup()
await framework.setup_a2a()

# A2A Agent 的能力自动注册为工具
# 命名为 a2a::<alias>::<skill_name>
# 模型可以像调用本地工具一样委托任务给远程 Agent
```

---

## 13. CLI 命令行

### 13.1 命令格式

```bash
agent-cli [OPTIONS]
```

### 13.2 选项

| 选项 | 说明 |
|---|---|
| `--config, -c <path>` | 配置文件路径 |
| `--model, -m <name>` | 覆盖模型名 |
| `--task, -t <text>` | 单次执行模式 |
| `--auto-approve` | 自动批准工具调用 |

### 13.3 REPL 内置命令

| 命令 | 说明 |
|---|---|
| `help` | 显示帮助 |
| `tools` | 列出已注册工具 |
| `memories` | 列出保存的记忆 |
| `exit` / `quit` / `q` | 退出 |

### 13.4 使用示例

```bash
# 使用 DeepSeek 交互
agent-cli -c config/deepseek.json

# 使用通义千问执行单次任务
agent-cli -c config/qwen.json -t "解释 Python 装饰器"

# 使用 GPT-4o 并自动批准工具
agent-cli -c config/openai.json --auto-approve
```

---

## 14. 编程接口详解

### 14.1 AgentFramework (主入口)

```python
from agent_framework.entry import AgentFramework

fw = AgentFramework(config_path="config.json")  # 或传入 FrameworkConfig 对象

# 初始化
fw.setup(agent=None, auto_approve_tools=False)

# 运行任务
result = await fw.run("你的任务描述")

# 注册工具
fw.register_tool(my_function)
fw.register_tools_from_module(my_module)

# MCP / A2A
await fw.setup_mcp()
await fw.setup_a2a()

# 记忆管理
fw.list_memories(user_id=None)
fw.forget_memory("id")
fw.pin_memory("id")
fw.clear_memories()

# 清理
await fw.shutdown()
```

### 14.2 AgentRunResult (运行结果)

```python
result = await fw.run("task")

result.success          # bool: 是否成功
result.final_answer     # str | None: 最终回答
result.run_id           # str: 运行 ID
result.stop_signal      # StopSignal: 停止原因
result.usage            # TokenUsage: token 消耗
result.iterations_used  # int: 迭代次数
result.error            # str | None: 错误信息
```

### 14.3 StopReason (停止原因)

| 值 | 说明 |
|---|---|
| `LLM_STOP` | 模型正常输出完毕 |
| `MAX_ITERATIONS` | 达到最大迭代次数 |
| `USER_CANCEL` | 用户取消 |
| `CUSTOM` | 自定义停止（如 ReAct Final Answer） |
| `ERROR` | 错误终止 |
| `OUTPUT_TRUNCATED` | 输出被截断 |

### 14.4 TokenUsage

```python
result.usage.prompt_tokens      # 输入 token
result.usage.completion_tokens  # 输出 token
result.usage.total_tokens       # 总计 token
```

---

## 15. 自定义 Agent

### 15.1 继承 BaseAgent

```python
from agent_framework.agent.base_agent import BaseAgent
from agent_framework.models.agent import (
    AgentConfig, AgentState, CapabilityPolicy,
    ErrorStrategy, IterationResult,
)
from agent_framework.models.message import ToolCallRequest

class MyAgent(BaseAgent):
    def __init__(self):
        config = AgentConfig(
            agent_id="my-agent",
            system_prompt="你是一个专业的代码审查助手。",
            model_name="deepseek-chat",
            max_iterations=15,
            temperature=0.3,
        )
        super().__init__(config)

    # --- 生命周期钩子 ---

    async def on_before_run(self, task, agent_state):
        """运行前初始化。"""
        print(f"开始处理任务: {task}")

    async def on_iteration_started(self, iteration_index, agent_state):
        """每次迭代开始。"""
        print(f"迭代 {iteration_index}")

    async def on_tool_call_requested(self, tool_call_request: ToolCallRequest) -> bool:
        """工具调用拦截器。返回 False 阻止调用。"""
        if tool_call_request.function_name == "dangerous_tool":
            return False
        return True

    async def on_tool_call_completed(self, tool_result):
        """工具调用完成回调。"""
        if not tool_result.success:
            print(f"工具失败: {tool_result.error}")

    async def on_final_answer(self, answer, agent_state):
        """最终回答生成后。"""
        print(f"回答: {answer[:100]}...")

    # --- 策略方法 ---

    def get_error_policy(self, error, agent_state) -> ErrorStrategy:
        """错误处理策略。"""
        if agent_state.iteration_count < 10:
            return ErrorStrategy.RETRY
        return ErrorStrategy.ABORT

    def get_capability_policy(self) -> CapabilityPolicy:
        """工具权限策略。"""
        return CapabilityPolicy(
            allowed_tool_categories=["search", "filesystem"],  # 仅允许这些类别
            allow_network_tools=True,
            allow_spawn=False,
        )
```

### 15.2 使用自定义 Agent

```python
agent = MyAgent()
framework.setup(agent=agent)
result = await framework.run("审查这段代码的安全性")
```

---

## 16. 架构概览

```
Entry           → entry.py (AgentFramework), cli.py (REPL)
                       │
Agent           → agent/ (BaseAgent, DefaultAgent, ReActAgent)
                       │
                → coordinator.py (RunCoordinator — 完整运行生命周期)
                → loop.py (AgentLoop — 单次迭代: LLM → 停止检查 → 工具)
                       │
SubAgent        → subagent/ (Factory, Scheduler, Runtime)
                       │
Tools           → tools/ (decorator, catalog, registry, executor)
                → delegation.py (DelegationExecutor — A2A + SubAgent 路由)
                       │
Memory          → memory/ (SQLiteStore, DefaultManager, MemoryScope)
                       │
Context         → context/ (SourceProvider, Builder, Compressor, Engineer)
                       │
Protocols       → protocols/ (MCP, A2A)
                       │
Adapters        → adapters/model/ (LiteLLM, OpenAI, Anthropic, Google, 国产模型)
                       │
Infrastructure  → infra/ (Config, Logger, EventBus, DiskStore)
                       │
Models          → models/ (Message, Tool, Agent, Session, Memory, SubAgent, Context)
```

### 关键数据流

```
用户输入
  → SkillRouter (技能检测)
  → ContextEngineer (上下文组装: 系统提示 + 记忆 + 历史 + 输入)
  → ModelAdapter (LLM 调用)
  → AgentLoop (停止条件检查)
  → ToolExecutor (工具执行: local / mcp / a2a / subagent)
  → SessionState (记录消息)
  → 循环 / 停止
  → MemoryManager (记忆提取)
  → AgentRunResult
```

---

## 17. 环境变量

| 变量 | 说明 | 适用 adapter_type |
|---|---|---|
| `OPENAI_API_KEY` | OpenAI API Key | `openai`, `litellm` |
| `ANTHROPIC_API_KEY` | Anthropic API Key | `anthropic` |
| `GOOGLE_API_KEY` | Google AI API Key | `google` |
| `DEEPSEEK_API_KEY` | DeepSeek API Key | `deepseek` |
| `VOLCENGINE_API_KEY` | 火山引擎 API Key | `doubao` |
| `DASHSCOPE_API_KEY` | 阿里云 DashScope API Key | `qwen` |
| `ZHIPU_API_KEY` | 智谱 API Key | `zhipu` |
| `MINIMAX_API_KEY` | MiniMax API Key | `minimax` |

配置文件中 `api_key` 字段为 `null` 时，各适配器会尝试从对应环境变量读取。也可在配置文件中直接设置 `api_key`。

---

## 18. 常见问题

### Q: 如何切换模型？

三种方式：
```bash
# 1. 配置文件
# 修改 config.json 的 model.adapter_type 和 model.default_model_name

# 2. 命令行覆盖
agent-cli -c config/openai.json --model gpt-4o-mini

# 3. 环境变量
export AGENT_MODEL__ADAPTER_TYPE=deepseek
export AGENT_MODEL__DEFAULT_MODEL_NAME=deepseek-chat
```

### Q: 如何使用本地模型（Ollama/vLLM）？

```json
{
  "model": {
    "adapter_type": "custom",
    "default_model_name": "llama3",
    "api_key": "not-needed",
    "api_base": "http://localhost:11434/v1"
  }
}
```

### Q: 如何禁用记忆？

```json
{
  "memory": {
    "enable_saved_memory": false,
    "auto_extract_memory": false
  }
}
```

或代码中：
```python
framework.set_memory_enabled(False)
```

### Q: 工具调用失败怎么办？

- Agent 默认使用 `ABORT` 策略终止
- ReActAgent 使用 `RETRY` 策略重试
- 自定义 Agent 可覆盖 `get_error_policy()` 控制策略

### Q: 如何查看详细日志？

```json
{
  "logging": {
    "level": "DEBUG",
    "json_output": false
  }
}
```

### Q: token 超出上下文限制怎么办？

调整 `context.max_context_tokens` 匹配你的模型上下文窗口。框架会自动压缩：
1. 先裁剪最旧的会话历史
2. 再截断过长的工具返回结果
3. 记忆和系统提示不被压缩

### Q: 如何运行测试？

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

当前测试覆盖 426 个测试用例，涵盖所有功能模块。

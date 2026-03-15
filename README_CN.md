# Aegis Agent Framework

> 离线优先、可扩展的 AI Agent 运行时 — Python 3.11+ / pydantic v2

一个工程级 Agent 框架，具备清晰的模块边界、结构化审计链路和多模型/多协议支持。涵盖单 Agent 工具调用、GPT 风格记忆系统、子 Agent 协调编排、MCP/A2A 协议集成 — 全部可在本地模型下离线运行。

---

## 快速开始

```bash
# 安装（开发模式）
pip install -e ".[dev]"

# 启动交互终端（Mock 模型，无需 API Key）
python -m agent_framework.main

# 使用真实模型运行
python -m agent_framework.main --config config/openai.json

# 运行演示
python run_demo.py

# 运行测试（757 项全部通过）
pytest tests/

# 使用 Textual TUI 运行（需安装）
pip install textual
python -m agent_framework.main --config config/deepseek.json
```

---

## 核心能力

### Agent 循环

- **ReAct** 模式，自动提取最终答案
- **6 层终止机制**：LLM_STOP / MAX_ITERATIONS / OUTPUT_TRUNCATED / ERROR / USER_CANCEL / 超时
- **终止分类**（TerminationKind）：NORMAL（正常完成）/ ABORT（硬失败）/ DEGRADE（软降级）
- 迭代历史作为只增（append-only）审计轨迹
- 结构化决策模型（`StopDecision`、`ToolCallDecision`、`SpawnDecision`）— 禁止裸 bool

### 工具系统

- 内置工具：`read_file`、`write_file`、`list_directory`、`run_command`、`spawn_agent` 等
- 命名规范：`local::<名称>` / `mcp::<服务>::<名称>` / `a2a::<别名>::<名称>`
- `@tool` 装饰器：函数 → 工具的自动转换，含参数 schema 生成
- 并发执行 + 串行副作用提交（`ToolCommitSequencer` 按输入顺序排序）
- 确认处理器（自动放行或 CLI 交互确认）
- 能力策略白名单交集语义（只能收窄，不能扩权）

### 上下文工程（5 槽模型）

| 槽位 | 内容 | 预算 |
|------|------|------|
| 1 | 系统核心提示 | 15% |
| 2 | 技能附加提示 | 5% |
| 3 | 已保存记忆 | 10% |
| 4 | 会话历史 | 60% |
| 5 | 当前输入 | 10% |

- 确定性输出：相同输入 = 相同提示（无随机性）
- 滑动窗口压缩：超出 token 预算时自动裁剪
- 只读合约：上下文层绝不修改任何状态
- **冻结前缀**：系统提示 + 技能 addon 生成不可变前缀，跨迭代复用提升 provider 端 KV cache 命中率
- **XML 结构化注入**：`<system-identity>` / `<agent-capabilities>` / `<available-skills>` / `<saved-memories>` 分区，LLM 可清晰区分各区域

### 记忆系统

- SQLite 持久化（`data/memories.db`）
- 基于规则的自动提取（偏好、约束、项目上下文）
- 来源追踪：user / agent / subagent / admin
- 置信度过滤：低置信度推断候选默认丢弃
- 治理接口：置顶、取消置顶、激活、停用、清空

### 会话历史持久化
- SQLite 存储（`data/memories.db`，与记忆系统共享同一数据库）
- **项目级隔离**：以运行目录名（如 `my-agent`）作为唯一 project ID
- **多窗口会话**：每次 `/reset` 创建新会话窗口，旧窗口保留在 DB 中可随时切换
- 退出时自动保存，启动时自动恢复（显示最近 3 轮摘要）
- 命令：`/sessions`（列出所有）、`/session-switch <id>`（切换）、`/reset`（新窗口）、`/history-clear`（删除当前）

### 交互命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示所有可用命令 |
| `/reset` | 保存当前会话，开启新上下文窗口 |
| `/sessions` | 列出当前项目的所有会话窗口 |
| `/session-switch <id>` | 切换到指定会话（支持 ID 前缀匹配） |
| `/history` | 查看对话历史 |
| `/history-clear` | 清空当前会话（内存 + DB） |
| `/tools` | 列出已注册工具 |
| `/skills` | 列出可用技能 |
| `/config` | 显示当前配置 |
| `/stats` | 显示上下文 token 统计 |
| `/compact` | LLM 压缩历史 |
| `/exit` | 保存并退出 |

### 多智能体协调

- **SubAgentFactory** 派生子 Agent，支持 3 种记忆模式：`ISOLATED`（隔离）/ `INHERIT_READ`（继承只读）/ `SHARED_WRITE`（共享写入）
- **调度/执行分离**：Scheduler 负责配额与排队，Runtime 负责执行与生命周期
- 任务状态机：`QUEUED → SCHEDULED → RUNNING → COMPLETED / FAILED / CANCELLED`
- 递归派生保护（强制 `allow_spawn_children=False`）
- 统一委派状态机（`SubAgentStatus`）：本地子 Agent 与 A2A 远程使用同一状态枚举

### 模型适配器（11 个）

| 适配器 | 类型 |
|--------|------|
| LiteLLM | 统一封装 |
| OpenAI | 原生 SDK |
| Anthropic | 原生 SDK |
| Google GenAI | 原生 SDK |
| DeepSeek（深度求索） | OpenAI 兼容 |
| Doubao（豆包） | OpenAI 兼容 |
| Qwen（通义千问） | OpenAI 兼容 |
| Zhipu（智谱） | OpenAI 兼容 |
| MiniMax | OpenAI 兼容 |
| Custom（自定义） | OpenAI 兼容模板 |

```bash
# 通过配置文件切换模型
python -m agent_framework.main --config config/deepseek.json
python -m agent_framework.main --config config/anthropic.json
```

### 上下文管理与 Token 优化

框架支持两种会话模式，区别在于**每轮发送给 API 的 `messages` 数组内容不同**。

**场景**：用户说”帮我读取 /tmp/test.txt”，模型调用 `read_file`，然后用户说”把内容改成 Hi”。

#### `stateless`（默认）— 每轮全量

```python
# ── Round 1: API 收到 ─────────────────────────────────
messages = [
    {“role”: “system”,    “content”: “<system-identity>...</system-identity>”},
    {“role”: “user”,      “content”: “帮我读取 /tmp/test.txt”},
]
# → 模型调用 read_file → 返回 “Hello World” → 回答 “文件内容是 Hello World”

# ── Round 2: API 收到 ─────────────────────────────────
messages = [
    {“role”: “system”,    “content”: “<system-identity>...</system-identity>”},    # ← 重复
    {“role”: “user”,      “content”: “帮我读取 /tmp/test.txt”},                    # ← 重复
    {“role”: “assistant”, “tool_calls”: [{“id”:”tc1”, “function”:{“name”:”read_file”,...}}]},  # ← 重复
    {“role”: “tool”,      “content”: “Hello World”, “tool_call_id”: “tc1”},        # ← 重复
    {“role”: “assistant”, “content”: “文件内容是 Hello World”},                     # ← 重复
    {“role”: “user”,      “content”: “把内容改成 Hi”},                             # ← 新增
]
# 6 条消息, ~3000 tokens — Round 1 的所有内容被重新发送
```

每轮重发：system prompt + 全部历史 + 当前输入。token 线性增长。
超出预算时，**滑动窗口压缩**自动裁剪最早的 messages。

#### `stateful`（可选）— 首轮全量，后续增量

```python
# ── Round 1: API 收到（与 stateless 相同）──────────────
messages = [
    {“role”: “system”,    “content”: “<system-identity>...</system-identity>”},
    {“role”: “user”,      “content”: “帮我读取 /tmp/test.txt”},
]
# sent_count: 0 → 2

# ── Round 2: API 收到（仅新增部分）─────────────────────
messages = [
    {“role”: “assistant”, “tool_calls”: [{“id”:”tc1”, “function”:{“name”:”read_file”,...}}]},  # ← 新增
    {“role”: “tool”,      “content”: “Hello World”, “tool_call_id”: “tc1”},                   # ← 新增
    {“role”: “assistant”, “content”: “文件内容是 Hello World”},                                 # ← 新增
    {“role”: “user”,      “content”: “把内容改成 Hi”},                                         # ← 新增
]
# 4 条消息, ~200 tokens — 无 system，无 Round 1 历史
# sent_count: 2 → 6
```

Provider 侧保持完整会话上下文，框架只发送 `messages[sent_count:]`。
压缩被**跳过**（裁剪会导致 sent_count 偏移错位）。

#### 配置

```json
{“model”: {“session_mode”: “stateless”}}
{“model”: {“session_mode”: “stateful”}}
```

默认 `stateless`。仅当 provider 确认支持服务端会话状态时才切换 `stateful`。

#### 底层管理差异

| 层 | `stateless` | `stateful` |
|----|------------|------------|
| **`get_delta_messages()`** | 返回完整数组 | 返回 `messages[sent_count:]` |
| **API 请求大小** | 线性增长（每轮包含全部历史） | 近似常数（仅新增消息） |
| **上下文压缩** | 启用 — 滑动窗口裁剪最早消息 | 跳过 — 裁剪会破坏增量偏移 |
| **`_session.active`** | `False` | `True` |
| **`sent_count` 追踪** | 不更新 | 每轮递增 |
| **provider 无状态时** | 正常工作 | 模型丢失上下文（请求中无历史） |

实现链路：
1. `RunCoordinator` 在 run 开始时调用 `adapter.begin_session(run_id)`
2. `ContextEngineer` 检查 `stateful_session` 标志 → 为 true 时跳过压缩
3. `AgentLoop._call_llm()` 调用 `adapter.get_delta_messages(messages)` → 将结果发送给 API
4. `adapter.end_session()` 在 `finally` 块中执行

| | stateless | stateful |
|--|-----------|----------|
| **Round 1** | ~2700 tokens | ~2700 tokens |
| **Round 2** | ~3000 tokens | **~200 tokens** |
| **Round 10** | ~6000 tokens | **~150 tokens** |

> 以上 token 数值为示意估算。

### 技能系统（SKILL.md）

```
skills/
├── commit/SKILL.md          ← Git 提交助手
├── explain-code/SKILL.md    ← 代码解释
└── review-pr/SKILL.md       ← 代码审查
```

- **文件发现**：`skills/`（项目级）+ `~/.agent/skills/`（个人级）
- **YAML 前置元数据**：name、description、allowed-tools、argument-hint
- **渐进式披露**：仅 description 注入上下文，body 在调用时才从磁盘加载
- **预处理**：`$ARGUMENTS` / `$0` / `$1` 参数替换 + `!`shell`` 命令执行
- **`${SKILL_DIR}`**：技能目录路径变量，支持引用附属文件
- **LLM 触发**：通过 `invoke_skill` 工具，LLM 根据 description 语义判断何时调用

### Orchestrator 多智能体编排

- **OrchestratorAgent**：编排感知 prompt，支持并行/串行子 agent 委派
- **动态能力注入**：`<agent-capabilities>` 实时注入 max_iterations / spawned_subagents / parallel_tool_calls
- **硬退出守卫**：spawn 后 3 轮无新 spawn 则强制停止（防 LLM 空转）
- **子 agent 清理**：run 退出时自动 cancel 所有活跃子 agent

### 协议集成

- **MCP**：客户端管理器，支持 stdio/SSE/HTTP 传输，自动发现工具
- **A2A**：跨机器 Agent RPC，统一错误码

---

## 架构总览

```
┌─────────────────────────────────────────────────┐
│  入口层 (entry.py, cli.py, main.py)             │
├─────────────────────────────────────────────────┤
│  Agent 层                                        │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────┐│
│  │RunCoordinator│ │RunStateCtrl  │ │PolicyRes.││
│  │  (编排调度)   │ │ (唯一写端口) │ │ (配置)   ││
│  └──────┬───────┘ └──────────────┘ └──────────┘│
│         │                                        │
│  ┌──────▼───────┐ ┌──────────────┐              │
│  │  AgentLoop   │ │MessageProject│              │
│  │ (迭代执行)   │ │  (消息格式化) │              │
│  └──────────────┘ └──────────────┘              │
├─────────────────────────────────────────────────┤
│  子Agent    │  工具        │  上下文  │  记忆   │
│  Factory    │  Executor    │  Engineer│  Manager│
│  Scheduler  │  Registry    │  Provider│  Store  │
│  Runtime    │  Delegation  │  Builder │  SQLite │
├─────────────────────────────────────────────────┤
│  适配器 (LiteLLM, OpenAI, Anthropic, Google)    │
├─────────────────────────────────────────────────┤
│  协议 (MCP Client, A2A Client)                  │
├─────────────────────────────────────────────────┤
│  基础设施 (Config, Logger, EventBus, DiskStore) │
└─────────────────────────────────────────────────┘
```

### 三层运行协调

| 层 | 角色 | 职责 |
|----|------|------|
| **RunCoordinator** | 编排器 | 决定**何时**变更状态 |
| **RunStateController** | 状态执行器 | 决定**如何**变更状态（唯一写端口） |
| **RunPolicyResolver** | 配置组合器 | 产出 `ResolvedRunPolicyBundle`（冻结后不可改） |

### 核心设计原则

- **Protocol → Base → Default** 三层模式：所有可扩展模块均遵循
- **不可变模型**：`EffectiveRunConfig`、`ToolMeta`、`ResolvedRunPolicyBundle` 全部 frozen
- **唯一写端口**：只有 `RunStateController` 可修改 `AgentState` / `SessionState`
- **策略解释权唯一**：ContextPolicy 只归 ContextEngineer 解释，MemoryPolicy 只归 MemoryManager 解释
- **事件仅可观测**：EventBus 订阅方禁止修改任何状态，投递语义为尽力而为
- **不支持恢复**：中断的 run 视为终止，继续执行必须创建新 run
- **重试需幂等声明**：`retryable=true` 不等于 `idempotent=true`，自动重试需要幂等保障

---

## 项目结构

```
agent_framework/
├── agent/           # Agent 循环、编排器、状态管理、技能路由
├── tools/           # 工具装饰器、注册表、执行器、委派
├── memory/          # 记忆管理器、SQLite 存储
├── context/         # 上下文工程、压缩、5 槽构建器
├── subagent/        # 子 Agent 工厂、调度器、运行时
├── models/          # pydantic v2 数据模型
├── protocols/       # MCP 客户端、A2A 客户端
├── adapters/model/  # LLM 适配器（11 个提供商）
├── infra/           # 配置、日志、事件总线
├── entry.py         # 框架入口门面
├── cli.py           # CLI 入口点
└── main.py          # 交互式终端
config/              # 模型配置文件（JSON）
tests/               # 757 项测试
```

---

## 配置

配置文件位于 `config/` 目录。示例（`config/openai.json`）：

```json
{
  "model": {
    "adapter_type": "openai",
    "model_name": "gpt-4",
    "api_key": "${OPENAI_API_KEY}"
  },
  "context": {
    "max_tokens": 8192
  },
  "memory": {
    "enabled": true,
    "db_path": "data/memories.db"
  }
}
```

可用配置：`openai`、`anthropic`、`google`、`deepseek`、`doubao`、`qwen`、`zhipu`、`minimax`、`custom`。

---

## 自定义工具

```python
from agent_framework.tools.decorator import tool

@tool(name="my_tool", category="general", description="一个有用的工具")
def my_tool(query: str, limit: int = 10) -> str:
    """搜索某些内容。"""
    return f"为 {query} 找到 {limit} 条结果"
```

通过 `AgentFramework.register_tool(my_tool)` 注册，或放入模块中在启动时注册。

---

## 扩展框架

### 自定义 Agent

```python
from agent_framework.agent.base_agent import BaseAgent

class MyAgent(BaseAgent):
    def should_stop(self, iteration_result, agent_state):
        # 自定义停止逻辑 — 必须返回 StopDecision，不可返回 bool
        ...

    async def on_tool_call_requested(self, tool_call_request):
        # 自定义工具审批 — 必须返回 ToolCallDecision
        ...
```

### 自定义模型适配器

实现 `ModelAdapterProtocol`：

```python
class MyAdapter:
    async def complete(self, messages, tools=None, temperature=None, max_tokens=None):
        ...  # → ModelResponse

    async def stream_complete(self, messages, tools=None):
        ...  # → AsyncIterator[ModelChunk]

    def count_tokens(self, messages):
        ...  # → int
```

### 自定义记忆存储

实现 `MemoryStoreProtocol`，传入 `DefaultMemoryManager(store=my_store)` 即可替换底层存储。

### 编程式使用（二次开发）

```python
import asyncio
from agent_framework.entry import AgentFramework
from agent_framework.infra.config import load_config
from agent_framework.tools.decorator import tool

# 1. 定义自定义工具
@tool(name="search", description="搜索知识库")
def search(query: str) -> str:
    return f"搜索结果: {query}"

# 2. 加载配置并初始化
config = load_config("config/deepseek.json")
fw = AgentFramework(config=config)
fw.setup(auto_approve_tools=True)
fw.register_tool(search)

# 3. 单次运行
async def main():
    result = await fw.run("Python 是什么？")
    print(result.final_answer)

    # 多轮对话（传入历史消息）
    result1 = await fw.run("读取 /tmp/test.txt")
    result2 = await fw.run(
        "总结一下内容",
        initial_session_messages=result1.session_messages,
    )
    print(result2.final_answer)
    await fw.shutdown()

asyncio.run(main())
```

### 流式输出

```python
async for event in fw.run_stream("解释 Python 的 async"):
    if event.type.name == "TOKEN":
        print(event.data["token"], end="", flush=True)
    elif event.type.name == "DONE":
        result = event.data["result"]
```

### 自定义技能（文件方式）

创建 `skills/my-skill/SKILL.md`：

```markdown
---
name: my-skill
description: 分析代码质量
allowed-tools: [read_file, list_directory]
---

分析 $ARGUMENTS 中的代码质量问题。
关注：命名规范、复杂度、错误处理。
```

交互终端中使用：`/skill my-skill src/main.py`，或编程注册：

```python
fw.register_skill(Skill(
    skill_id="my-skill",
    name="代码质量",
    description="分析代码质量",
    system_prompt_addon="你是一个代码质量审查员...",
))
```

### 嵌入 Web 应用

```python
from fastapi import FastAPI
from agent_framework.entry import AgentFramework

app = FastAPI()
fw = AgentFramework(config=load_config("config/openai.json"))
fw.setup(auto_approve_tools=True)

@app.post("/chat")
async def chat(message: str, session_id: str | None = None):
    # 从你的会话存储加载历史消息
    prior_messages = load_session(session_id) if session_id else []
    result = await fw.run(
        message,
        initial_session_messages=prior_messages,
        user_id="web-user",
    )
    # 保存更新后的消息到你的会话存储
    save_session(session_id, result.session_messages)
    return {"answer": result.final_answer}
```

### 关键扩展点

| 扩展方向 | 协议/基类 | 注入方式 |
|----------|-----------|----------|
| Agent 行为 | `BaseAgent` | `fw.setup(agent=MyAgent(...))` |
| 模型提供商 | `ModelAdapterProtocol` | `fw._deps.model_adapter = MyAdapter()` |
| 记忆存储 | `MemoryStoreProtocol` | `DefaultMemoryManager(store=...)` |
| 工具 | `@tool` 装饰器 | `fw.register_tool(fn)` |
| 技能 | `Skill` 模型 | `fw.register_skill(skill)` |
| MCP 服务 | 配置 JSON | `fw.config.mcp.servers` |

---

## 测试

```bash
# 全量测试（757 项）
pytest tests/

# 仅运行架构守卫测试
pytest tests/test_architecture_guard.py -v

# 特定模块测试
pytest tests/test_agent.py -v
pytest tests/test_tools.py -v
pytest tests/test_subagent.py -v
```

测试分类：

| 类别 | 说明 | 数量 |
|------|------|------|
| 单元测试 | Agent、工具、记忆、上下文、子 Agent 各模块 | ~300 |
| 红线测试 | v2.5.2 – v2.6.5 架构边界断言 | 106 |
| 架构守卫 | 反旁路扫描 + 故障注入 + 数据流不变量 | 43 |
| 集成测试 | 完整 run 生命周期、模型适配器冒烟测试 | ~300 |

### 架构守卫覆盖（test_architecture_guard.py）

**反旁路扫描**（20 项）：
- SessionState 写端口合规（仅 RunStateController 可写）
- AgentLoop 零状态写入（不碰 status/tokens/history）
- 策略解释权隔离（coordinator 不读 policy 字段）
- TransactionGroupIndex 消费合规（不重建事务组）
- SubAgent 所有权分离（Scheduler 无 active_children）

**故障注入**（11 项）：
- 模型 API 500 → ERROR 终止 + 记忆会话仍关闭
- 工具部分失败 → 成功与失败结果均投影
- 子 Agent 超时 → 正确返回失败
- 记忆提交失败 → 不阻断 run
- 外部取消 → USER_CANCEL + ABORT
- 全局超时 → MAX_ITERATIONS + DEGRADE

**数据流不变量**（12 项）：
- 迭代历史只增不减
- 快照冻结后不反映后续变更
- 提交排序按 input_index
- 重试版本链 parent_attempt_id 关联

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11+ |
| 数据模型 | pydantic v2 |
| 配置管理 | pydantic-settings |
| 结构化日志 | structlog |
| 事件总线 | blinker |
| LLM 路由 | litellm |
| 持久化 | SQLite |
| 协议支持 | MCP SDK, A2A SDK |
| 测试框架 | pytest, pytest-asyncio |

---

## 安装选项

```bash
# 仅核心
pip install -e .

# 含开发工具
pip install -e ".[dev]"

# 选装适配器
pip install -e ".[openai,anthropic,mcp]"

# 全部安装
pip install -e ".[all]"
```

---

## 许可证

详见 [LICENSE](LICENSE)。

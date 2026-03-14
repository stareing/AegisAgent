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

# 运行测试（678 项全部通过）
pytest tests/
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

### 上下文压缩与会话模式

框架支持两种会话模式，通过配置切换：

```json
{"model": {"session_mode": "stateless"}}
{"model": {"session_mode": "stateful"}}
```

#### 方案 A：STATELESS（默认，兼容所有 provider）

每轮向模型发送完整 messages 列表，包含系统提示 + 全部历史 + 当前输入：

```
Round 1 请求体:
  [system: 完整系统提示]              ← ~2000 tokens
  [user: "你好"]                      ← ~5 tokens
  总计: ~2700 tokens (含工具 schema)

Round 2 请求体:
  [system: 完整系统提示]              ← 重复发送
  [user: "你好"]                      ← 重复发送
  [assistant: "你好呀！"]             ← 历史
  [user: "1+1等于"]                   ← 当前输入
  总计: ~2900 tokens

Round 3 请求体:
  [system: 完整系统提示]              ← 重复发送
  [user: "你好"]                      ← 重复发送
  [assistant: "你好呀！"]             ← 重复发送
  [user: "1+1等于"]                   ← 重复发送
  [assistant: "等于2"]               ← 历史
  [user: "再见"]                      ← 当前输入
  总计: ~3100 tokens
```

- 每轮 token 随历史线性增长
- 超出预算时启用上下文压缩（滑动窗口裁剪旧消息 / 工具结果截断）
- Provider 可能在服务端做前缀缓存（如 Anthropic prompt caching），但客户端无法控制

#### 方案 B：STATEFUL（首轮全量 + 后续增量，需 provider 支持）

首轮发送完整上下文，后续仅发送新增消息：

```
Round 1 请求体:
  [system: 完整系统提示]              ← 仅此轮发送
  [user: "你好"]
  总计: ~2700 tokens

Round 2 请求体:                       ← 无 system，无历史
  [assistant: "你好呀！"]             ← 上轮模型回复
  [user: "1+1等于"]                   ← 当前输入
  总计: ~100 tokens                   ← 节省 96%

Round 3 请求体:
  [assistant: "等于2"]               ← 上轮模型回复
  [user: "再见"]                      ← 当前输入
  总计: ~50 tokens
```

- 后续轮次 token 消耗接近常数（仅新消息）
- 跳过上下文压缩（provider 侧保持完整上下文，压缩会破坏增量索引）
- 系统提示 / 技能 / 模型状态变化时自动重建会话

#### 模式对比

| | STATELESS | STATEFUL |
|--|-----------|----------|
| **token 趋势** | 线性增长 | 近似常数 |
| **压缩** | 启用（sliding_window / tool_summary） | 跳过 |
| **兼容性** | 所有 provider | 需要 provider 维持服务端上下文 |
| **适用** | 通用场景、短对话 | 多轮长对话、token 敏感 |
| **配置** | `"session_mode": "stateless"` | `"session_mode": "stateful"` |

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
tests/               # 577 项测试
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

---

## 测试

```bash
# 全量测试（577 项）
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
| 单元测试 | Agent、工具、记忆、上下文、子 Agent 各模块 | ~250 |
| 红线测试 | v2.5.2 – v2.6.5 架构边界断言 | 106 |
| 架构守卫 | 反旁路扫描 + 故障注入 + 数据流不变量 | 43 |
| 集成测试 | 完整 run 生命周期、模型适配器冒烟测试 | ~180 |

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

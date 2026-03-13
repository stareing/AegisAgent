# AI Agent Framework - CLAUDE.md

* **单一职责**：一个模块、类、函数只负责一类清晰职责。
* **禁止重复造轮子**：优先复用成熟开源方案，非核心能力不自研。
* **导入前置**：`import` 原则上统一放在文件头部。
* **最小暴露**：非公开能力默认私有，减少无必要的对外接口。
* **命名清晰**：名称必须表达职责，避免缩写和含糊命名。
* **显式优于隐式**：禁止依赖隐藏副作用和隐式状态流转。
* **类型优先**：公开接口必须补全类型标注。
* **数据与行为分离**：数据模型不承载复杂业务逻辑。
* **面向接口编程**：依赖抽象，不直接耦合具体实现。
* **默认不可变**：能不用可变状态就不用可变状态。
* **异常要分类**：不要抛裸异常，错误类型要明确。
* **失败可解释**：错误信息必须可读、可定位、可处理。
* **函数尽量短小**：单个函数尽量只完成一个完整动作。
* **避免深层嵌套**：优先早返回，减少多层 `if/else`。
* **禁止魔法值**：重复使用的常量必须提取命名。
* **配置外置**：可变参数放配置，不写死在逻辑中。
* **副作用集中**：I/O、网络、数据库调用集中在边界层。
* **注释解释原因**：注释优先说明“为什么”，不是重复“做什么”。
* **兼容性优先**：公共接口变更必须考虑向后兼容。
* **测试友好**：设计必须便于 mock、替换和单元测试。
* **边界清晰**：跨层调用必须通过正式接口，禁止越层访问。
* **一处定义**：同一规则、常量、协议只保留一个权威定义。
* **记忆进度**：热跟新CLAUDE.md
* **代码审核**：将由codex排查代码是否和需求一致

## Project Overview
Offline-first, extensible AI Agent Framework built in Python 3.11+ with pydantic v2.
Architecture follows Protocol / Base / Default three-layer pattern for all extensible modules.

## Tech Stack
- Python 3.11+, pydantic v2, pydantic-settings
- structlog for logging, blinker for events
- litellm for unified LLM adapter
- SQLite for offline memory storage
- MCP SDK (`mcp`) for tool discovery from external servers
- A2A SDK (`a2a-python`) for inter-agent delegation

## Architecture Layers (bottom → top)
```
Entry           → entry.py (AgentFramework facade), cli.py (REPL + argparse)
Agent           → agent/ (base_agent, default_agent, react_agent, loop, coordinator, runtime_deps, skill_router, capability_policy)
SubAgent        → subagent/ (memory_scope, factory, scheduler, runtime)
Tools           → tools/ (decorator, catalog, registry, executor, confirmation, delegation, builtin/spawn_agent)
Memory          → memory/ (sqlite_store, base_manager, default_manager)
Context         → context/ (transaction_group, source_provider, builder, compressor, engineer)
Protocols       → protocols/ (core.py, mcp/mcp_client_manager.py, a2a/a2a_client_adapter.py)
Models          → models/ (message, tool, agent, session, memory, subagent, context, mcp)
Adapters        → adapters/model/ (base_adapter, litellm_adapter, openai_adapter, anthropic_adapter, google_adapter, openai_compatible_adapter)
Infrastructure  → infra/ (config, logger, event_bus, disk_store)
```

## Development Progress

### Completed (Layers 1-8: Infrastructure → Agent)
- [x] `infra/` — FrameworkConfig, StructLogger, EventBus, DiskStore
- [x] `models/` — All 7 model modules (message, tool, agent, session, memory, subagent, context)
- [x] `protocols/core.py` — All 10 Protocol definitions
- [x] `adapters/model/` — BaseModelAdapter ABC + LiteLLMAdapter with retry/streaming
- [x] `tools/` — @tool decorator, GlobalToolCatalog, ToolRegistry (qualified naming), ToolExecutor (local/mcp/a2a/subagent routing), ConfirmationHandlers, DelegationExecutor
- [x] `memory/` — SQLiteMemoryStore, BaseMemoryManager, DefaultMemoryManager (pattern extraction + merge rules)
- [x] `context/` — ToolTransactionGroup, ContextSourceProvider, ContextBuilder (5-slot), ContextCompressor, ContextEngineer
- [x] `agent/` — BaseAgent (7 hooks + 4 strategies), DefaultAgent, AgentLoop, RunCoordinator, SkillRouter, CapabilityPolicy
- [x] Consistency verification against architecture doc v2.3 — 13 gaps found and fixed

### Completed (Tasks #11-#16: ReAct + MCP + A2A + SubAgent + Entry)
- [x] **#11** ReAct Agent (`agent/react_agent.py`) — ReAct system prompt, "Final Answer:" detection, retry error policy
- [x] **#12** SubAgent Runtime — `subagent/memory_scope.py` (Isolated/InheritRead/SharedWrite), `factory.py`, `scheduler.py` (quota + timeout), `runtime.py` (facade) + `tools/builtin/spawn_agent.py`
- [x] **#13** MCP Client (`protocols/mcp/client_manager.py` + `models/mcp.py`) — stdio/sse/streamable_http transport, tool discovery + sync to catalog
- [x] **#14** A2A Client (`protocols/a2a/client_adapter.py`) — agent discovery via agent cards, task delegation, tool sync
- [x] **#15** Entry Layer + CLI (`entry.py` AgentFramework facade + `cli.py` REPL with argparse)
- [x] **#16** Integration wiring — delegation.py A2A wiring, pyproject.toml optional deps (mcp/a2a/all), `agent-cli` script entry point

### Completed (Tasks #17-#22: 多智能体协调)
- [x] **#17** SubAgentScheduler API 对齐 doc 14.4 — `submit()` + `await_result()` 分离式接口，保留 `schedule()` 便利方法
- [x] **#18** Executor subagent 路由完善 — 传递所有 spawn_agent 参数 (task_input, mode, skill_id, tool_categories, memory_scope)
- [x] **#19** `build_spawn_seed()` 添加到 ContextBuilder (doc 8.8) — 从父会话中选取最近消息种子给子Agent上下文
- [x] **#20** 派生权限检查 (doc 16.2/20.2) — DelegationExecutor 检查 allow_spawn_children，子Agent调spawn_agent返回 PERMISSION_DENIED
- [x] **#21** 多智能体协调演示 — run_demo.py 新增 demo_9(子Agent派生) + demo_10(递归防护)
- [x] **#22** spawn_agent 工具 source="subagent" 正确路由，ContextBuilder 构造函数支持 max_context_tokens/reserve_for_output

### Completed (Tasks #23-#27: v2.4 架构升级)
- [x] **#23** 新增 `ContextPolicy`, `MemoryPolicy`, `EffectiveRunConfig` pydantic 模型 (v2.4 §3)
- [x] **#24** SessionState 写入权限收归 RunCoordinator 独占 (v2.4 §4)
- [x] **#25** AgentLoop 最小依赖传递 — 只接收 model_adapter + tool_executor，不接收完整 AgentRuntimeDeps (v2.4 §5)
- [x] **#26** RunCoordinator: EffectiveRunConfig 构建 + Skill 反激活保证（正常/异常路径均清理）(v2.4 §8/§9/§18)
- [x] **#27** MemoryScope 快照语义 — InheritRead/SharedWrite 在 spawn 时刻捕获父记忆冻结快照，运行期间不感知父记忆变化 (v2.4 §10)

### Completed (Task #29: 多模型 SDK 适配)
- [x] **#29** 原生 SDK 适配器 — OpenAIAdapter / AnthropicAdapter / GoogleAdapter
  - `adapters/model/openai_adapter.py` — 官方 openai SDK，格式与 LiteLLM 兼容
  - `adapters/model/anthropic_adapter.py` — 官方 anthropic SDK，处理 content blocks、system 分离、tool_result 合并
  - `adapters/model/google_adapter.py` — 官方 google-genai SDK，处理 role 映射、function_call/response parts、合成 tool_call_id
  - `ModelConfig.adapter_type` 字段选择适配器 ("litellm"|"openai"|"anthropic"|"google")
  - `entry.py` 适配器工厂方法 `_create_model_adapter()`，懒加载 SDK
  - `__init__.py` 条件导入，缺少 SDK 不影响框架启动
  - `pyproject.toml` 可选依赖组: `[openai]`, `[anthropic]`, `[google]`, `[all]`

### Completed (Task #30: 国产模型 + 自定义适配)
- [x] **#30** OpenAI-compatible 适配器 — DeepSeek / 豆包 / 通义千问 / 智谱 / MiniMax / Custom
  - `adapters/model/openai_compatible_adapter.py` — `OpenAICompatibleAdapter` 基类 + 6 个子类
  - 各厂商预设 `api_base` 和默认模型名：DeepSeek(`deepseek-chat`), Doubao(`doubao-pro-32k`), Qwen(`qwen-plus`), Zhipu(`glm-4`), MiniMax(`abab6.5s-chat`)
  - `CustomAdapter` 支持任意 OpenAI-compatible 端点（需手动传 `api_base`）
  - 中文文本 token 估算优化（CJK ~1.5 chars/token）
  - `extra_headers` 支持（部分厂商需自定义 Header）
  - `config.model.adapter_type` 新增: `"deepseek"|"doubao"|"qwen"|"zhipu"|"minimax"|"custom"`
  - `entry.py` 工厂方法扩展，全部走 `openai` SDK（无额外依赖）

### Completed (Task #31: 全模块严格测试)
- [x] **#31** 严格单元测试覆盖全部功能模块 — 426 tests total
  - `test_tools.py` (80 tests) — @tool 装饰器、Catalog、Registry、Executor、Delegation、CapabilityPolicy
  - `test_memory.py` (55 tests) — SQLiteStore CRUD、DefaultManager 抽取/合并、MemoryScope 快照语义
  - `test_context.py` (30 tests) — TransactionGroup、SourceProvider、Builder 5-slot 组装/裁剪/spawn seed、Compressor
  - `test_agent.py` (35 tests) — BaseAgent hooks、DefaultAgent、ReActAgent、SkillRouter、AgentLoop、RunCoordinator
  - `test_subagent.py` (20 tests) — Scheduler 配额/超时/取消、Factory 内存域/工具过滤/快照捕获
  - `test_infra.py` (18 tests) — EventBus 发布/订阅、DiskStore JSON/文本/原子写入
  - `test_openai_compatible_adapters.py` (30 tests) — 6 个厂商默认值、构建参数、token 计数、complete/retry、Entry 工厂

### Completed (Task #32-#34: Skill 系统修复 + 交互终端)
- [x] **#32** Skill 公开 API — `AgentFramework.register_skill()`, `list_skills()`, `remove_skill()`, `get_active_skill()`
- [x] **#33** Skill 配置加载 — `SkillConfig` / `SkillsConfig` 模型，`FrameworkConfig.skills` 字段，setup() 自动加载
- [x] **#34** CLI skills 命令 — REPL 新增 `skills` 命令，显示注册技能列表和激活状态
- [x] **#35** 交互终端 `main.py` — 彩色 ANSI 输出、命令系统（help/tools/skills/memories/config/stats/clear/reset）、内置 Mock 模型 + 3 个 demo 工具 + 3 个 demo 技能
- [x] **#36** Skill 演示 — run_demo.py 新增 demo_11（技能注册→检测→激活→反激活完整流程）
- [x] **#37** 入口点 — `python -m agent_framework.main` + `agent-interactive` CLI 命令

### Bug Fixes (本轮)
- [x] SubAgentFactory 使用 DefaultAgent 构造函数替代 `__new__` + `BaseAgent.__init__` 直接调用
- [x] SubAgentFactory 清理冗余 AgentConfig，移除未使用参数
- [x] SubAgentFactory 默认阻止子Agent访问 system/network/subagent 类工具 (doc 2.6/20.1)
- [x] MemoryScope managers 补充 `extract_candidates()` 抽象方法实现
- [x] ContextEngineer.build_spawn_seed 委托给 ContextBuilder（消除重复逻辑）
- [x] run_demo.py SmartMockModel `_tool_results` 累积 bug 修复（跟踪 `_last_seen_msg_count`）
- [x] structlog 日志噪音抑制（demo 中设 WARNING 级别）

## Key Design Patterns
- **Qualified tool naming**: `local::<name>`, `mcp::<server_id>::<name>`, `a2a::<alias>::<name>`, `subagent::spawn_agent`
- **Context slots**: System Core → Skill Addon → Saved Memories → Session History → Current Input
- **Tool permission chain**: CapabilityPolicy → ScopedToolRegistry → on_tool_call_requested()
- **Memory scopes for subagents**: ISOLATED, INHERIT_READ, SHARED_WRITE (v2.4: spawn-time frozen snapshot for reads)
- **Error strategies**: ABORT, SKIP, RETRY (per agent policy)
- **子Agent递归防护**: SubAgentFactory 强制 allow_spawn_children=False，DelegationExecutor 检查并返回 PERMISSION_DENIED
- **子Agent派生流**: spawn_agent tool_call → ToolExecutor(source=subagent) → DelegationExecutor → SubAgentRuntime → Factory + Scheduler
- **多模型适配**: BaseModelAdapter ABC → LiteLLM/OpenAI/Anthropic/Google + DeepSeek/Doubao/Qwen/Zhipu/MiniMax/Custom，`config.model.adapter_type` 选择
- **OpenAI-compatible 模式**: 国产模型均通过 `openai` SDK + 自定义 `base_url` 接入，`OpenAICompatibleAdapter` 基类统一管理

## Commands
```bash
# Install
pip install -e ".[dev]"

# Tests
pytest tests/

# Interactive terminal (Mock 模型, 无需 API Key)
python -m agent_framework.main

# Interactive terminal (真实模型)
python -m agent_framework.main --config config/deepseek.json

# Demo (Mock 模型)
python run_demo.py
```

## File Conventions
- All models use pydantic v2 BaseModel
- All config uses pydantic-settings BaseSettings
- TYPE_CHECKING imports for forward refs to avoid circular imports
- Protocols are runtime_checkable
- Async tool functions detected automatically by @tool decorator

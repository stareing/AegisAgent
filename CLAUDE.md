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
* **日志结构化**：日志要可检索、可追踪、可关联 run_id。
* **注释解释原因**：注释优先说明“为什么”，不是重复“做什么”。
* **兼容性优先**：公共接口变更必须考虑向后兼容。
* **测试友好**：设计必须便于 mock、替换和单元测试。
* **默认安全**：高风险操作默认拒绝，显式授权后再放行。
* **边界清晰**：跨层调用必须通过正式接口，禁止越层访问。
* **一处定义**：同一规则、常量、协议只保留一个权威定义。
* **记忆进度**：热跟新CLAUDE.md

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
Adapters        → adapters/model/ (base_adapter, litellm_adapter)
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
- **Memory scopes for subagents**: ISOLATED, INHERIT_READ, SHARED_WRITE
- **Error strategies**: ABORT, SKIP, RETRY (per agent policy)
- **子Agent递归防护**: SubAgentFactory 强制 allow_spawn_children=False，DelegationExecutor 检查并返回 PERMISSION_DENIED
- **子Agent派生流**: spawn_agent tool_call → ToolExecutor(source=subagent) → DelegationExecutor → SubAgentRuntime → Factory + Scheduler

## Commands
```bash
# Install
pip install -e ".[dev]"

# Tests (when available)
pytest tests/
```

## File Conventions
- All models use pydantic v2 BaseModel
- All config uses pydantic-settings BaseSettings
- TYPE_CHECKING imports for forward refs to avoid circular imports
- Protocols are runtime_checkable
- Async tool functions detected automatically by @tool decorator

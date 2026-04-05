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

### Completed (Task #38-#39: 终止条件闭环 + 详细日志)
- [x] **#38** 终止条件统一闭环修复
  - `coordinator.py`: 全局 `run_timeout_ms` 超时（默认5分钟），防止主Agent无限挂起
  - `coordinator.py`: `cancel_event: asyncio.Event` 支持外部取消，触发 `USER_CANCEL` StopReason
  - `loop.py`: `finish_reason="stop"` + tool_calls 边界修复 — 优先终止，丢弃工具调用
  - `loop.py`: 重复工具调用检测 — 连续3次相同调用自动强制 ERROR 停止
  - `loop.py`: 连续3次 LLM 错误强制 ABORT（防止 RETRY 策略死循环）
  - `executor.py`: spawn_agent 路由时传递 `parent_run_id`，修复子Agent配额跟踪失效
- [x] **#39** 全链路详细日志增强
  - `loop.py`: iteration 级日志含 max_iterations/tokens/context_messages/tools_available
  - `loop.py`: LLM 调用日志含 temperature/token 分项/response_preview/tool_names
  - `loop.py`: 工具执行日志含 execution_time_ms/source/output_preview
  - `coordinator.py`: run 级日志含 model/max_iterations/allow_spawn/elapsed_ms
  - `executor.py`: 工具路由日志含 source/arguments_keys/subagent详情
  - `delegation.py`: 子Agent委托全流程日志（requested/approved/denied/hook_denied）
  - `runtime.py`: 子Agent生命周期日志（spawning/creating/created/quota_check/run_started/finished）
  - `scheduler.py`: 任务状态日志（running/completed/timeout/cancelled/failed/quota_exceeded）
  - `logger.py`: STANDARD_EVENTS 从17个扩展到50+个

### Completed (Task #41: 架构审查 6 项边界强化)
- [x] **#41.8** iteration_history 不可改写审计轨迹 — RunStateController 文档化 append-only 合约，success/fail/retry/skip 均入历史，context 压缩不改写
- [x] **#41.9** ToolResult.output 序列化边界 — `_sanitize_output()` 强制 JSON 可序列化, 50K 字符截断, 不可序列化对象自动转 str
- [x] **#41.10** 日志/运行态边界硬约束 — logger.py 模块级文档: 日志仅服务观测, 业务判断禁止依赖日志内容, 日志缺失不影响正确性
- [x] **#41.11** Artifact 生命周期边界 — content 限小对象, 大对象用 uri, 生命周期由产出方 runtime 管理, memory 层只吸收摘要
- [x] **#41.12** ConfirmationHandler 确认决策分层 — `_should_confirm()` + `CapabilityPolicy.force_confirm_categories` 策略级升级, Handler 只执行流程
- [x] **#41.13** ScopedToolRegistry 可见性 vs 执行边界 — docstring 明确: Registry=可见性优化, ToolExecutor.is_tool_allowed()=安全边界
- [x] **#41.14** A2A/subagent 统一失败语义 — `DelegationErrorCode` 枚举(TIMEOUT/QUOTA_EXCEEDED/PERMISSION_DENIED/DELEGATION_FAILED/REMOTE_UNAVAILABLE), A2A 错误统一映射

### Completed (Task #40: 架构审查 7 项改进)
- [x] **#40.1** RunCoordinator 职责拆分 — 新增 `RunStateController`（状态变更）和 `RunPolicyResolver`（配置合成），RunCoordinator 仅负责编排
- [x] **#40.2** 消息投影规则形式化 — `RunStateController.project_iteration_to_session()` 定义严格投影合约（assistant→tool顺序, error必投影, 不静默丢弃）
- [x] **#40.3** Policy 消费边界写死 — ContextPolicy 仅 ContextEngineer 消费, MemoryPolicy 仅 MemoryManager 消费, RunCoordinator 只传递不解释
- [x] **#40.4** EffectiveRunConfig 不可变 — `model_config = {"frozen": True}`，构建后禁止修改，防止运行时统计污染
- [x] **#40.5** CapabilityPolicy 双重执行点 — schema导出时可见性过滤（非安全边界）+ ToolExecutor.is_tool_allowed() 执行时校验（安全边界）
- [x] **#40.6** remember() 来源审计 — `MemorySourceContext(source_type, source_run_id, source_spawn_id)`, SharedWrite 强制标记 source_type="subagent"
- [x] **#40.7** 记忆治理工具拦截 — `CapabilityPolicy.allow_memory_admin` 默认 False, category="memory" 的工具被 apply_capability_policy 过滤

### Completed (Task #42: 架构审查 Bug修复 + 7 项边界强化)
- [x] **Bug** `dedup_guard` ValidationError — `ToolExecutionMeta(source="dedup_guard")` 不在 Literal 范围内，改为 `source="local"`
- [x] **#42.15** Message metadata 边界 — docstring 明确: metadata 不发送给 LLM，仅 role/content/tool_calls/tool_call_id/name 为 LLM 安全字段
- [x] **#42.16** ToolMeta 注册后冻结 — `model_config = {"frozen": True}`，@tool 装饰器新增 `source` 参数避免创建后变更
- [x] **#42.17** 记忆抽取触发边界 — DefaultMemoryManager 文档化: record_turn() 为唯一抽取入口，过滤低置信度推断候选
- [x] **#42.18** MemoryCandidate 置信度 — `MemoryCandidateSource` (EXPLICIT_USER/INFERRED/TOOL_DERIVED/ADMIN) + `MemoryConfidence` (HIGH/MEDIUM/LOW)
- [x] **#42.19** ContextSourceProvider 格式稳定性 — 确定性排序 (pinned→kind→title alphabetical)，相同输入产生相同输出
- [x] **#42.20** SubAgentFactory 反膨胀边界 — 文档化职责上限 (~200行)，超出需拆分为 PolicyResolver + DependencyBuilder
- [x] **#42.21** Framework Core vs Integration Layer 边界 — entry.py 文档化: Core=runtime/tools/context/memory, Integration=auth/UI/DTOs/streaming

### Bug Fixes (本轮)
- [x] SubAgentFactory 使用 DefaultAgent 构造函数替代 `__new__` + `BaseAgent.__init__` 直接调用
- [x] SubAgentFactory 清理冗余 AgentConfig，移除未使用参数
- [x] SubAgentFactory 默认阻止子Agent访问 system/network/subagent 类工具 (doc 2.6/20.1)
- [x] MemoryScope managers 补充 `extract_candidates()` 抽象方法实现
- [x] ContextEngineer.build_spawn_seed 委托给 ContextBuilder（消除重复逻辑）
- [x] run_demo.py SmartMockModel `_tool_results` 累积 bug 修复（跟踪 `_last_seen_msg_count`）
- [x] structlog 日志噪音抑制（demo 中设 WARNING 级别）
- [x] `_rl_wrap` regex 替换崩溃修复 — `\x01`/`\x02` 在 re.sub 替换串中是非法转义，改用 lambda
- [x] SubAgentSpec.parent_run_id 未传递导致配额跟踪失效 — executor 路由时从 parent_agent 获取

## Key Design Patterns
- **Qualified tool naming**: `local::<name>`, `mcp::<server_id>::<name>`, `a2a::<alias>::<name>`, `subagent::spawn_agent`
- **Context slots**: System Core → Skill Addon → Saved Memories → Session History → Current Input
- **Tool permission chain (双重执行点)**: schema导出过滤(可见性) → ToolExecutor.is_tool_allowed()(安全边界) → on_tool_call_requested()
- **RunCoordinator 三层分离**: RunCoordinator(编排) + RunStateController(状态变更) + RunPolicyResolver(配置合成)
- **Policy 消费边界**: ContextPolicy→ContextEngineer独占, MemoryPolicy→MemoryManager独占, RunCoordinator只传递不解释
- **EffectiveRunConfig 不可变**: frozen=True, 构建后只读, 不承载运行统计
- **记忆写入审计**: MemorySourceContext 区分 user/agent/subagent/admin 来源, SharedWrite 强制标记 subagent
- **iteration_history 审计轨迹**: append-only, 不可改写, success/fail/retry/skip 均入历史, context 压缩不影响
- **ToolResult.output 序列化边界**: 必须 JSON 可序列化, ToolExecutor._sanitize_output() 强制截断/转换
- **日志≠状态**: 日志仅观测/审计, 业务判断禁止依赖日志, 日志缺失不影响正确性
- **确认决策分层**: CapabilityPolicy.force_confirm_categories(策略升级) > ToolMeta.require_confirm(工具声明) > 默认不确认; Handler 只执行流程
- **委派失败统一语义**: DelegationErrorCode 枚举统一本地 subagent 和远程 A2A 的错误码 (TIMEOUT/QUOTA_EXCEEDED/PERMISSION_DENIED/DELEGATION_FAILED/REMOTE_UNAVAILABLE)
- **Memory scopes for subagents**: ISOLATED, INHERIT_READ, SHARED_WRITE (v2.4: spawn-time frozen snapshot for reads)
- **Error strategies**: ABORT, SKIP, RETRY (per agent policy, 连续3次错误强制 ABORT)
- **终止条件闭环 (6层)**:
  1. `LLM_STOP` — finish_reason="stop" 无 tool_calls（含 stop+tool_calls 边界修复）
  2. `MAX_ITERATIONS` — 迭代次数硬限制（主20/子10）
  3. `OUTPUT_TRUNCATED` — finish_reason="length"
  4. `ERROR` — 不可恢复错误/连续3次错误/重复工具调用3次
  5. `USER_CANCEL` — asyncio.Event 外部取消
  6. `run_timeout_ms` — 全局墙钟超时（默认5分钟）
- **子Agent递归防护**: SubAgentFactory 强制 allow_spawn_children=False，DelegationExecutor 检查并返回 PERMISSION_DENIED
- **子Agent派生流**: spawn_agent tool_call → ToolExecutor(source=subagent) → DelegationExecutor → SubAgentRuntime → Factory + Scheduler
- **多模型适配**: BaseModelAdapter ABC → LiteLLM/OpenAI/Anthropic/Google + DeepSeek/Doubao/Qwen/Zhipu/MiniMax/Custom，`config.model.adapter_type` 选择
- **ToolMeta 不可变**: frozen=True, 注册后不可修改, 可见性可变但合约不可变
- **MemoryCandidate 写入控制**: CandidateSource+Confidence 区分来源和置信度, 低置信推断自动过滤
- **格式稳定性**: ContextSourceProvider 确定性输出 — 相同输入恒等输出, 无随机/时间/调用者依赖
- **Framework/Integration 边界**: Core=agent runtime+tools+context+memory, Integration=auth+UI+DTOs+streaming+deployment
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

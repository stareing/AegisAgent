# AI Agent Framework — 开发实现文档 v2.4

> **定位**：从 0 开发、离线优先、边界清晰、默认简单且可替换的 Agent 框架实现文档
> **版本**：v2.4
> **目标**：单 Agent / 工具调用 / Session 管理 / GPT 风格 Saved Memory / 子 Agent / 多协议接入
> **约束**：完全离线可运行 · 无远端数据库依赖 · 无向量数据库依赖 · 无嵌入 API · 默认记忆仅依赖本地数据库

---

## 一、文档目标

本文档用于指导一个 **从 0 开发** 的 AI Agent Framework 实现。目标不是提供概念性架构草图，而是给出一份可以直接开始编码的工程设计文档。

本文档重点解决四类问题：

* 核心运行边界清晰
* 基类、协议、默认实现职责明确
* 默认实现足够简单，离线可运行
* 高级能力可替换，不绑死用户技术路线

默认实现语言为 Python 3.11+，核心数据模型基于 `pydantic v2`。

---

## 二、核心设计原则

### 2.1 开源优先

成熟能力直接集成，不重复造轮子。框架自实现范围仅限于：

* 运行时编排
* Agent 循环驱动
* 工具注册与执行胶水层
* 上下文组织与压缩协调
* Saved Memory 抽象与默认实现
* 子 Agent 运行时
* MCP / A2A 协议胶水层

### 2.2 离线优先

默认能力必须在离线环境下可运行：

* 本地模型可用时即可完整运行
* 默认记忆基于本地数据库
* 默认配置从本地文件读取
* 默认日志写本地磁盘

### 2.3 Base / Protocol / Default 三层明确分工

所有可扩展模块按三层定义：

#### `*Protocol`

定义最小契约，用于解耦与类型约束。

#### `Base*`

定义推荐扩展骨架，承载公共逻辑、默认钩子和扩展模板。

#### `Default*`

框架官方默认实现，目标是简单、可解释、可替换。

**约束**：

* 只想兼容框架，可直接实现 `Protocol`
* 想复用默认流程，应继承 `Base*`
* 框架内置实现统一放在 `Default*`

### 2.4 关注点分离

* 编排层只负责运行时调度
* 工具层只负责注册、校验、执行、路由
* 上下文层只负责素材拼装和裁剪
* 记忆层只负责 Saved Memory 的治理
* 协议层只负责与 MCP / A2A 胶合
* 基础设施层只负责配置、日志、事件、磁盘

### 2.5 默认简单，扩展开放

默认实现必须足够简单，不引入过度复杂设计。高级需求通过替换协议实现，而不是把默认主干做成“大而全平台”。

### 2.6 默认安全收敛

* 子 Agent 默认不可递归派生
* 子 Agent 默认不可使用 `system / network` 类工具
* 高风险工具必须显式确认
* Saved Memory 默认只做只读注入，不自动共享写入

---

## 三、术语定义

### 3.1 Session

一次完整的 Agent 运行上下文，从 `begin_session()` 到 `end_session()`。

### 3.2 Conversation Turn

一次用户输入到 Agent 产出最终回答的完整闭环。一个 turn 可能包含多个 iteration。

### 3.3 Iteration

`AgentLoop` 的一次推理-行动循环：

1. 组装 LLM 请求
2. 调用模型
3. 判断停止条件
4. 执行工具调用
5. 返回结构化迭代结果

### 3.4 Session History

当前 run 的消息历史，只服务于当前上下文构建，不属于长期记忆。

### 3.5 Saved Memory

长期保留的结构化记忆，接近 GPT 风格的已保存记忆，主要包括：

* 用户偏好
* 用户长期约束
* 长期项目背景
* 稳定事实
* 可复用任务提示

### 3.6 Tool Transaction Group

上下文裁剪时不可拆分的消息事务组，至少包括：

* 一条 assistant 的 `tool_calls` 与其对应全部 tool 消息
* assistant 文本与其同条消息中的 tool_calls
* `spawn_agent` 请求与其返回结果

---

## 四、总体架构

```text
┌────────────────────────────────────────────────────────────────┐
│                      接入层 Entry Layer                        │
│                 CLI / REST API / SDK / REPL                   │
├────────────────────────────────────────────────────────────────┤
│                运行时编排层 Orchestration Layer                │
│ BaseAgent ← RunCoordinator ← AgentLoop ← SkillRouter          │
│        │                    │                                  │
│ AgentRuntimeDeps       SessionState                            │
│                             │                                  │
│                 SubAgentRuntime / DelegationExecutor           │
├──────────────┬───────────────┬──────────────┬─────────────────┤
│   工具层      │   记忆层       │   上下文层    │   协议层         │
│ Tool Layer   │ Memory Layer  │ Context Layer│ Protocol Layer  │
│ Decorator    │ BaseManager    │ SourceProv.  │ MCP Client      │
│ Catalog      │ StoreProtocol  │ Builder      │ A2A Client      │
│ Registry     │ DefaultMemory  │ Compressor   │                 │
│ Executor     │                │ Engineer     │                 │
├──────────────┴───────────────┴──────────────┴─────────────────┤
│                 模型适配层 Model Adapter Layer                 │
│           ModelAdapter ← LiteLLMAdapter / Others              │
├────────────────────────────────────────────────────────────────┤
│                基础设施层 Infrastructure Layer                 │
│         Config / StructLogger / EventBus / DiskStore          │
└────────────────────────────────────────────────────────────────┘
```

---

## 五、v2.4 边界修订清单（正式并入文档）

### 缺陷 1：`BaseAgent` 同时承担策略、依赖容器、扩展点，边界过宽

修订：新增 `AgentRuntimeDeps` 统一承载运行依赖；`BaseAgent` 聚焦于策略与业务钩子，不再承担依赖容器职责。

### 缺陷 2：`Protocol` 与 `Base*` 的分工不明确

修订：文档明确规定 `Protocol` 定义最小契约，`Base*` 提供推荐扩展骨架，`Default*` 提供官方默认实现。

### 缺陷 3：`MemoryStoreProtocol` 混入了相似度判断等业务逻辑

修订：去除存储层的“相似判断”职责；去重、合并、冲突处理全部回收到 `BaseMemoryManager`。

### 缺陷 4：记忆层直接返回 `formatted_block`，与上下文层重新耦合

修订：记忆层只返回 `MemoryRecord` 列表；具体格式化统一由 `ContextSourceProvider` 完成。

### 缺陷 5：Session History 没有最小归属对象

修订：新增 `SessionState`，作为当前 run 的消息历史唯一持有者；它不属于记忆层，也不属于上下文层。

### 缺陷 6：`CapabilityPolicy`、whitelist、ScopedRegistry 三套权限机制重叠

修订：定义统一优先级：`CapabilityPolicy` 决定能力上界，`ScopedToolRegistry` 决定当前可见集，`on_tool_call_requested()` 作为最终运行时拦截。

### 缺陷 7：Skill 的 model override 生效边界不清晰

修订：Skill override 只在当前 run 的 `effective config` 中生效，不直接修改 `agent_config`，子 Agent 默认不继承。

### 缺陷 8：MemoryScope 在插件化记忆场景下语义不落地

修订：明确父子调用路径：子 Agent 永远不直接拿到父 store 的写权限；`SHARED_WRITE` 只能通过父 `MemoryManager.remember()` 写回。

### 缺陷 9：`spawn_agent` 内部仍与普通工具执行语义混杂

修订：新增 `DelegationExecutorProtocol`，把“委派执行”从普通工具执行语义中单独抽象出来。

### 缺陷 10：缺少用户侧的记忆治理边界

修订：补充记忆治理接口：列出、删除、启停、钉住、清空、总开关。

### 缺陷 11：`SessionState` 虽已存在，但消息写入责任未唯一化

修订：规定 `RunCoordinator` 是 `SessionState` 的唯一写入协调者；`AgentLoop` 只返回 `IterationResult`，`ToolExecutor` 不直接写会话消息。

### 缺陷 12：`AgentRuntimeDeps` 有退化为 Service Locator 的风险

修订：增加最小依赖传递约束：除 `RunCoordinator` 外，任何模块不得继续下传完整 `AgentRuntimeDeps`，下层只接收自身必需依赖。

### 缺陷 13：`ContextPolicy` 与 `MemoryPolicy` 被引用但未定义

修订：补充两个 policy 数据模型，并明确它们属于 run-scoped policy，而不是进程级全局配置。

### 缺陷 14：`effective config` 边界不完整

修订：新增 `EffectiveRunConfig`，明确其与 `FrameworkConfig`、`AgentConfig` 的分层关系，并限定 Skill override 只能覆盖白名单字段。

### 缺陷 15：`DelegationExecutorProtocol` 与 `SubAgentRuntimeProtocol` 边界重叠

修订：明确 `DelegationExecutor` 是委派统一入口，`SubAgentRuntime` 只负责本地子 Agent 生命周期；A2A 委派也统一经由 `DelegationExecutor` 路由。

### 缺陷 16：`MemoryScope` 未定义读路径是快照还是实时视图

修订：规定子 Agent 默认读取父记忆的只读快照，而不是实时视图，保证运行稳定性与可重放性。

### 缺陷 17：记忆治理接口默认暴露边界不明

修订：记忆治理接口属于用户显式控制面，默认不暴露给 LLM；若要暴露，必须通过单独管理工具并要求确认。

### 缺陷 18：Skill 的去激活责任人不明确

修订：规定 Skill 的激活与去激活均由 `RunCoordinator` 独占负责，在正常结束和异常结束路径都必须执行清理。

---

## 六、基础设施层

### 6.1 配置管理 `Config`

#### 开源借鉴

* `pydantic-settings`

#### 顶级配置

```python
class FrameworkConfig(BaseSettings):
    model: ModelConfig
    context: ContextConfig
    memory: MemoryConfig
    tools: ToolConfig
    subagent: SubAgentConfig
    mcp: MCPConfig
    a2a: A2AConfig
    logging: LoggingConfig
```

#### `ModelConfig`

* `default_model_name`
* `temperature`
* `max_output_tokens`
* `api_base`
* `timeout_ms`
* `max_retries`

#### `ContextConfig`

* `max_context_tokens`
* `reserve_for_output`
* `compress_threshold_ratio`
* `default_compression_strategy`
* `spawn_seed_ratio`

#### `MemoryConfig`

* `db_path`
* `enable_saved_memory`
* `auto_extract_memory`
* `max_memories_in_context`
* `max_memory_items_per_user`
* `allow_user_memory_namespace`
* `allow_memory_management_api`

#### `ToolConfig`

* `confirmation_handler_type`
* `max_concurrent_tool_calls`
* `allow_parallel_tool_calls`

#### `SubAgentConfig`

* `max_sub_agents_per_run`
* `max_concurrent_sub_agents`
* `per_sub_agent_max_tokens`
* `default_deadline_ms`
* `default_max_iterations`
* `allow_recursive_spawn`

#### `LoggingConfig`

* `log_dir`
* `json_output`
* `level`

#### 方法

* `load_config(config_path) -> FrameworkConfig`
* `reload_config() -> None`

---

### 6.2 结构化日志 `StructLogger`

#### 开源借鉴

* `structlog`

#### 标准字段

* `timestamp`
* `level`
* `run_id`
* `parent_run_id`
* `spawn_id`
* `iteration_index`
* `event`
* `duration_ms`
* `error_code`

#### 标准事件

* `run.started`
* `run.finished`
* `run.failed`
* `iteration.started`
* `iteration.completed`
* `llm.called`
* `llm.responded`
* `tool.dispatched`
* `tool.completed`
* `tool.failed`
* `context.compressed`
* `memory.saved`
* `memory.updated`
* `memory.deleted`
* `subagent.spawned`
* `subagent.completed`
* `subagent.failed`

---

### 6.3 事件总线 `EventBus`

#### 开源借鉴

* `blinker`

#### 方法

* `subscribe(event_name, handler)`
* `publish(event_name, payload)`
* `unsubscribe(event_name, handler)`

---

### 6.4 本地磁盘存储 `DiskStore`

#### 方法

* `write_json(path, data)`
* `read_json(path)`
* `write_text(path, text)`
* `read_text(path)`
* `ensure_directory(path)`
* `list_files(directory, pattern)`
* `atomic_write(path, content)`

---

## 七、数据模型层

所有模型使用 `pydantic v2`。

### 7.1 模型分层

#### Domain Models

框架内部业务对象。

#### DTO Models

跨进程、跨协议、API 暴露对象。

#### Persistence Models

数据库或文件持久化对象。

---

### 7.2 消息与模型响应

#### `Message`

* `role: Literal["system", "user", "assistant", "tool"]`
* `content: str | None`
* `tool_calls: list[ToolCallRequest] | None`
* `tool_call_id: str | None`
* `name: str | None`
* `metadata: dict | None`

#### `ToolCallRequest`

* `id: str`
* `function_name: str`
* `arguments: dict`

#### `TokenUsage`

* `prompt_tokens: int`
* `completion_tokens: int`
* `total_tokens: int`

#### `ModelResponse`

* `content: str | None`
* `tool_calls: list[ToolCallRequest]`
* `finish_reason: Literal["stop", "tool_calls", "length", "error"]`
* `usage: TokenUsage`
* `raw_response_meta: dict | None`

---

### 7.3 工具相关模型

#### `ToolMeta`

* `name: str`
* `description: str`
* `parameters_schema: dict`
* `category: str`
* `require_confirm: bool`
* `is_async: bool`
* `tags: list[str]`
* `source: Literal["local", "mcp", "a2a", "subagent"]`
* `namespace: str | None`
* `mcp_server_id: str | None`
* `a2a_agent_url: str | None`

#### `ToolEntry`

* `meta: ToolMeta`
* `callable_ref: Callable | None`
* `validator_model: type[BaseModel] | None`

#### `FieldError`

* `field: str`
* `expected: str | None`
* `received: str | None`
* `message: str`

#### `ToolExecutionError`

* `error_type: Literal["VALIDATION_ERROR", "EXECUTION_ERROR", "PERMISSION_DENIED", "NOT_FOUND", "TIMEOUT", "QUOTA_EXCEEDED"]`
* `error_code: str`
* `message: str`
* `field_errors: list[FieldError] | None`
* `retryable: bool`

#### `ToolResult`

* `tool_call_id: str`
* `tool_name: str`
* `success: bool`
* `output: Any`
* `error: ToolExecutionError | None`

#### `ToolExecutionMeta`

* `execution_time_ms: int`
* `source: Literal["local", "mcp", "a2a", "subagent"]`
* `trace_ref: str | None`
* `retry_count: int`

---

### 7.4 运行态模型

#### `AgentStatus`

* `IDLE`
* `RUNNING`
* `TOOL_CALLING`
* `SPAWNING`
* `FINISHED`
* `ERROR`
* `PAUSED`

#### `StopReason`

* `LLM_STOP`
* `MAX_ITERATIONS`
* `USER_CANCEL`
* `CUSTOM`
* `ERROR`
* `OUTPUT_TRUNCATED`

#### `StopSignal`

* `reason: StopReason`
* `message: str | None`

#### `IterationError`

* `error_type: str`
* `error_message: str`
* `retryable: bool`
* `stacktrace: str | None`

#### `IterationResult`

* `iteration_index: int`
* `model_response: ModelResponse | None`
* `tool_results: list[ToolResult]`
* `tool_execution_meta: list[ToolExecutionMeta]`
* `stop_signal: StopSignal | None`
* `error: IterationError | None`

#### `AgentState`

* `run_id: str`
* `task: str`
* `status: AgentStatus`
* `iteration_count: int`
* `turn_count: int`
* `total_tokens_used: int`
* `active_skill_id: str | None`
* `spawn_count: int`
* `iteration_history: list[IterationResult]`

#### `AgentRunResult`

* `run_id: str`
* `success: bool`
* `final_answer: str | None`
* `stop_signal: StopSignal`
* `usage: TokenUsage`
* `iterations_used: int`
* `artifacts: list[Artifact]`
* `error: str | None`

#### `ContextPolicy`

run-scoped 上下文策略对象。

* `allow_compression: bool`
* `prefer_recent_history: bool`
* `max_session_groups: int | None`
* `force_include_saved_memory: bool`

#### `MemoryPolicy`

run-scoped 记忆策略对象。

* `memory_enabled: bool`
* `auto_extract: bool`
* `allow_overwrite_pinned: bool`
* `allow_auto_save_from_tools: bool`

#### `EffectiveRunConfig`

当前 run 的最终生效配置，由 `RunCoordinator` 基于 `FrameworkConfig + AgentConfig + Skill override` 构建。

* `model_name: str`
* `temperature: float`
* `max_output_tokens: int`
* `max_iterations: int`
* `reserve_for_output: int`
* `max_concurrent_tool_calls: int`
* `subagent_token_budget: int`
* `allow_parallel_tool_calls: bool`

---

### 7.5 Session 相关模型

#### `SessionState`

当前 run 的会话状态唯一持有者。

* `session_id: str`
* `run_id: str`
* `messages: list[Message]`
* `started_at: datetime`
* `last_updated_at: datetime`

#### 方法语义

`SessionState` 只负责：

* 追加 user/assistant/tool 消息
* 返回消息历史
* 为上下文层提供消息源

`SessionState` 不负责：

* Saved Memory
* 长期持久化
* 检索
* 提示词格式化

#### 写入责任边界

* `RunCoordinator` 是 `SessionState` 的唯一写入协调者
* `AgentLoop` 只返回 `IterationResult`
* `ToolExecutor` 不直接写会话消息
* `RunCoordinator._record_iteration()` 统一将 `ModelResponse` 与 `ToolResult` 投影为 `Message` 并写入 `SessionState`

---

### 7.6 记忆相关模型

#### `MemoryKind`

* `USER_PROFILE`
* `USER_PREFERENCE`
* `USER_CONSTRAINT`
* `PROJECT_CONTEXT`
* `TASK_HINT`
* `CUSTOM`

#### `MemoryRecord`

默认 Saved Memory 条目。

* `memory_id: str`
* `user_id: str | None`
* `agent_id: str`
* `kind: MemoryKind`
* `title: str`
* `content: str`
* `tags: list[str]`
* `is_active: bool`
* `is_pinned: bool`
* `source: str | None`
* `created_at: datetime`
* `updated_at: datetime`
* `last_used_at: datetime | None`
* `use_count: int`
* `version: int`
* `extra: dict | None`

#### `MemoryCandidate`

* `kind: MemoryKind`
* `title: str`
* `content: str`
* `tags: list[str]`
* `reason: str | None`

#### `MemoryUpdateAction`

* `UPSERT`
* `DELETE`
* `IGNORE`

---

### 7.7 子 Agent 模型

#### `SpawnMode`

* `EPHEMERAL`
* `FORK`
* `LONG_LIVED`

#### `MemoryScope`

* `ISOLATED`
* `INHERIT_READ`
* `SHARED_WRITE`

#### `SubAgentSpec`

* `parent_run_id: str`
* `spawn_id: str`
* `mode: SpawnMode`
* `task_input: str`
* `agent_config_override: dict`
* `skill_id: str | None`
* `tool_category_whitelist: list[str] | None`
* `context_seed: list[Message] | None`
* `memory_scope: MemoryScope`
* `token_budget: int`
* `max_iterations: int`
* `deadline_ms: int`
* `allow_spawn_children: bool`

#### `Artifact`

* `artifact_type: str`
* `name: str`
* `uri: str | None`
* `content: dict | str | None`
* `metadata: dict | None`

#### `SubAgentHandle`

* `sub_agent_id: str`
* `spawn_id: str`
* `parent_run_id: str`
* `status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"]`
* `created_at: datetime`

#### `SubAgentResult`

* `spawn_id: str`
* `success: bool`
* `final_answer: str | None`
* `error: str | None`
* `artifacts: list[Artifact]`
* `usage: TokenUsage`
* `iterations_used: int`
* `duration_ms: int`
* `trace_ref: str | None`

#### `DelegationSummary`

* `status: str`
* `summary: str`
* `artifacts_digest: list[str]`
* `error_code: str | None`

---

## 八、Protocol 与基类设计

### 8.1 运行依赖容器 `AgentRuntimeDeps`

为避免 `BaseAgent` 退化成依赖容器，引入独立依赖承载对象。

#### 字段

* `tool_registry: ToolRegistryProtocol`
* `tool_executor: ToolExecutorProtocol`
* `memory_manager: MemoryManagerProtocol`
* `context_engineer: ContextEngineerProtocol`
* `model_adapter: ModelAdapterProtocol`
* `skill_router: SkillRouterProtocol`
* `confirmation_handler: ConfirmationHandlerProtocol`
* `sub_agent_runtime: SubAgentRuntimeProtocol | None`
* `delegation_executor: DelegationExecutorProtocol | None`

#### 依赖传递约束

* `RunCoordinator` 可以持有完整 `AgentRuntimeDeps`
* 其他模块不得继续向下传递完整 `AgentRuntimeDeps`
* 下层对象只接收自身最小必需依赖
* 禁止通过 `deps` 形成跨层随意调用

---

### 8.2 模型协议 `ModelAdapterProtocol`

#### 方法

* `complete(messages, tools, temperature, max_tokens) -> ModelResponse`
* `stream_complete(messages, tools) -> AsyncIterator[ModelChunk]`
* `count_tokens(messages) -> int`
* `supports_parallel_tool_calls() -> bool`

### 8.3 工具注册协议 `ToolRegistryProtocol`

#### 方法

* `get_tool(name) -> ToolEntry`
* `has_tool(name) -> bool`
* `list_tools(category=None, tags=None, source=None) -> list[ToolEntry]`
* `export_schemas(whitelist=None) -> list[dict]`

### 8.4 工具执行协议 `ToolExecutorProtocol`

#### 方法

* `execute(tool_call_request) -> tuple[ToolResult, ToolExecutionMeta]`
* `batch_execute(tool_call_requests) -> list[tuple[ToolResult, ToolExecutionMeta]]`

### 8.5 委派执行协议 `DelegationExecutorProtocol`

把“普通工具执行”和“Agent 委派执行”分开。

#### 边界

* `DelegationExecutor` 是所有委派动作的统一入口
* `ToolExecutor` 不直接处理子 Agent 生命周期或 A2A 远端生命周期
* `SubAgentRuntime` 只负责本地子 Agent 生命周期
* `A2AClientAdapter` 只负责远端 Agent 协议调用

#### 方法

* `delegate_to_subagent(spec: SubAgentSpec, parent_agent: BaseAgent) -> SubAgentResult`
* `delegate_to_a2a(agent_url: str, task_input: str, skill_id: str | None = None) -> SubAgentResult`

### 8.6 记忆存储协议 `MemoryStoreProtocol`

存储层只负责读写，不承担相似判断、去重或上下文格式化职责。

#### 方法

* `save(record: MemoryRecord) -> str`
* `update(record: MemoryRecord) -> None`
* `delete(memory_id: str) -> None`
* `get(memory_id: str) -> MemoryRecord | None`
* `list_by_user(agent_id: str, user_id: str | None, active_only: bool = True) -> list[MemoryRecord]`
* `list_by_kind(agent_id: str, user_id: str | None, kind: MemoryKind) -> list[MemoryRecord]`
* `list_recent(agent_id: str, user_id: str | None, limit: int) -> list[MemoryRecord]`
* `touch(memory_id: str) -> None`
* `count(agent_id: str, user_id: str | None) -> int`

### 8.7 记忆管理协议 `MemoryManagerProtocol`

#### 方法

* `begin_session(run_id: str, agent_id: str, user_id: str | None) -> None`
* `select_for_context(task: str, agent_state: AgentState) -> list[MemoryRecord]`
* `record_turn(user_input: str, final_answer: str | None, iteration_results: list[IterationResult]) -> None`
* `remember(candidate: MemoryCandidate) -> str | None`
* `forget(memory_id: str) -> None`
* `list_memories(agent_id: str, user_id: str | None) -> list[MemoryRecord]`
* `pin(memory_id: str) -> None`
* `unpin(memory_id: str) -> None`
* `activate(memory_id: str) -> None`
* `deactivate(memory_id: str) -> None`
* `clear_memories(agent_id: str, user_id: str | None) -> int`
* `set_enabled(enabled: bool) -> None`
* `end_session() -> None`

### 8.8 上下文协议 `ContextEngineerProtocol`

#### 方法

* `prepare_context_for_llm(agent_state, context_materials) -> list[Message]`
* `set_skill_context(skill_prompt: str | None) -> None`
* `build_spawn_seed(session_messages, query, token_budget) -> list[Message]`
* `report_context_stats() -> ContextStats`

### 8.9 子 Agent 运行时协议 `SubAgentRuntimeProtocol`

#### 方法

* `spawn(spec: SubAgentSpec, parent_agent: BaseAgent) -> SubAgentResult`
* `get_active_children(parent_run_id: str) -> list[SubAgentHandle]`
* `cancel_all(parent_run_id: str) -> int`

---

## 九、模型适配层

### 9.1 开源借鉴

* `litellm`
* `tiktoken`

### 9.2 `ModelAdapter` 抽象基类

#### 方法

* `complete(messages, tools, temperature, max_tokens) -> ModelResponse`
* `stream_complete(messages, tools) -> AsyncIterator[ModelChunk]`
* `count_tokens(messages) -> int`
* `supports_parallel_tool_calls() -> bool`

### 9.3 `LiteLLMAdapter`

#### 规则

* 指数退避重试
* 统一异常映射
* tool arguments JSON 解析失败时不抛出，只返回空 dict 并告警

#### 异常类型

* `LLMCallError`
* `LLMRateLimitError`
* `LLMAuthError`
* `LLMTimeoutError`

---

## 十、工具层

### 10.1 `@tool` 装饰器

#### 行为

1. 读取 docstring 第一段作为 description
2. 根据函数签名生成临时 `pydantic` 参数模型
3. 调用 `model_json_schema()` 生成 JSON Schema
4. 自动识别 async
5. 在函数上挂 `__tool_meta__`

#### 可选参数

* `name`
* `description`
* `category`
* `require_confirm`
* `tags`
* `namespace`

---

### 10.2 工具目录与注册表

#### `GlobalToolCatalog`

进程级工具总目录，不直接给 Agent 使用。

#### `ToolRegistry`

某个运行实例的工具快照视图。

#### `ScopedToolRegistry`

只读白名单视图，用于在当前 run 中裁剪可见工具集。

---

### 10.3 命名策略

内部工具名统一采用：

* 本地：`local::<name>`
* MCP：`mcp::<server_id>::<name>`
* A2A：`a2a::<agent_alias>::<name>`
* 子 Agent：`subagent::spawn_agent`

---

### 10.4 权限边界优先级

工具权限统一按以下优先级生效：

1. `CapabilityPolicy`：定义能力上界
2. `ScopedToolRegistry`：定义当前 run 可见工具集
3. `on_tool_call_requested()`：定义最终运行时拦截

**约束**：

* `get_tool_whitelist()` 不再作为独立权限层存在
* whitelist 只能作为生成 `ScopedToolRegistry` 的输入
* 任何下层不能突破上层限制

---

### 10.5 `ToolExecutor`

#### 方法

* `execute(tool_call_request) -> tuple[ToolResult, ToolExecutionMeta]`
* `batch_execute(tool_call_requests) -> list[tuple[ToolResult, ToolExecutionMeta]]`
* `_validate_arguments(tool_entry, arguments) -> dict | ToolExecutionError`
* `_route_execution(tool_entry, validated_arguments) -> Any`
* `_handle_tool_error(tool_name, error) -> tuple[ToolResult, ToolExecutionMeta]`

#### 路由规则

* `local` -> 本地函数
* `mcp` -> `MCPClientManager.call_mcp_tool()`
* `a2a` -> `DelegationExecutor.delegate_to_a2a()`
* `subagent` -> `DelegationExecutor.delegate_to_subagent()`

#### 参数校验错误返回格式

```json
{
  "error_type": "VALIDATION_ERROR",
  "error_code": "INVALID_ARGUMENT_TYPE",
  "message": "Field 'count' must be integer",
  "field_errors": [
    {
      "field": "count",
      "expected": "integer",
      "received": "string",
      "message": "Input should be a valid integer"
    }
  ],
  "retryable": true
}
```

---

## 十一、记忆层（简化版，可插件化）

### 11.1 设计目标

默认记忆只解决三类问题：

1. 记住用户稳定偏好
2. 记住长期约束和项目背景
3. 在后续对话中把这些信息简洁注入上下文

默认不做：

* 全文检索
* 排序召回流水线
* embedding 检索
* 自动存全部聊天
* session history 数据库化

---

### 11.2 GPT 风格 Saved Memory 原则

#### 应保存

* 用户偏好的输出方式
* 语言偏好、称呼偏好
* 长期项目背景
* 稳定约束
* 高复用任务提示

#### 不应保存

* 一次性临时问题
* 完整聊天记录
* 工具原始输出
* 中间推理过程
* 低置信度猜测

---

### 11.3 默认数据库表结构

默认实现使用 SQLite。

#### 表 `saved_memories`

* `memory_id TEXT PRIMARY KEY`
* `agent_id TEXT NOT NULL`
* `user_id TEXT NULL`
* `kind TEXT NOT NULL`
* `title TEXT NOT NULL`
* `content TEXT NOT NULL`
* `tags TEXT NOT NULL`
* `is_active INTEGER NOT NULL`
* `is_pinned INTEGER NOT NULL`
* `source TEXT NULL`
* `created_at TEXT NOT NULL`
* `updated_at TEXT NOT NULL`
* `last_used_at TEXT NULL`
* `use_count INTEGER NOT NULL`
* `version INTEGER NOT NULL`
* `extra TEXT NULL`

#### 索引建议

* `(agent_id, user_id, is_active)`
* `(agent_id, user_id, kind)`
* `(agent_id, user_id, updated_at DESC)`

---

### 11.4 `BaseMemoryManager`

这是用户最可能重写的高层基类。

#### 责任

* 判断什么值得记住
* 决定是否更新、覆盖或忽略记忆
* 决定哪些记忆进入当前上下文
* 提供用户治理接口

#### 不负责

* prompt 格式化
* session history 管理
* 模型调用
* 工具执行

#### 方法

* `begin_session(run_id, agent_id, user_id) -> None`
* `select_for_context(task, agent_state) -> list[MemoryRecord]`
* `record_turn(user_input, final_answer, iteration_results) -> None`
* `extract_candidates(user_input, final_answer, iteration_results) -> list[MemoryCandidate]`
* `merge_candidate(candidate, existing_records) -> MemoryUpdateAction`
* `remember(candidate) -> str | None`
* `forget(memory_id) -> None`
* `pin(memory_id) -> None`
* `unpin(memory_id) -> None`
* `activate(memory_id) -> None`
* `deactivate(memory_id) -> None`
* `clear_memories(agent_id, user_id) -> int`
* `set_enabled(enabled: bool) -> None`
* `end_session() -> None`

---

### 11.5 `DefaultMemoryManager`

默认实现遵循“简单、可解释、易替换”。

#### 默认行为

1. 每个 turn 结束后检查是否出现可保存信息
2. 提取少量 `MemoryCandidate`
3. 从 store 读取候选合并范围
4. 执行去重/更新/忽略
5. 下次上下文构建时，按简单规则挑选少量记忆注入

#### 默认提取规则

优先提取：

* “以后都用中文回答我”
* “我正在做一个离线 Agent 框架”
* “不要使用向量数据库”
* “我更关注 base class 和 protocol”

默认忽略：

* 临时问法
* 闲聊
* 工具输出细节

---

### 11.6 去重与合并策略

不使用复杂检索，仅使用简单规则：

1. 同 `kind + 归一化 title` 命中已有条目时，进入更新逻辑
2. 同 `content` 完全一致时忽略
3. `is_pinned=True` 的条目不自动覆盖
4. 新内容与旧内容冲突时，提升 `version`

**约束**：

* 去重逻辑只在 `MemoryManager` 中实现
* `MemoryStoreProtocol` 不做“相似判断”

---

### 11.7 上下文注入选择规则

不做全文搜索，默认只按以下规则选择：

1. `is_pinned=True` 的记忆优先
2. 与当前 task 关键词显式匹配的记忆
3. 最近更新的活跃记忆
4. 总数量限制在 `max_memories_in_context`

**边界**：

* 记忆层只返回 `list[MemoryRecord]`
* 记忆的文本格式化由上下文层负责

---

### 11.8 `MemoryStoreProtocol` 的默认实现：`SQLiteMemoryStore`

#### 方法

* `save(record) -> str`
* `update(record) -> None`
* `delete(memory_id) -> None`
* `get(memory_id) -> MemoryRecord | None`
* `list_by_user(agent_id, user_id, active_only=True) -> list[MemoryRecord]`
* `list_by_kind(agent_id, user_id, kind) -> list[MemoryRecord]`
* `list_recent(agent_id, user_id, limit) -> list[MemoryRecord]`
* `touch(memory_id) -> None`
* `count(agent_id, user_id) -> int`

#### 设计要求

* 只负责数据库读写
* 不负责候选提取
* 不负责上下文格式化
* 不负责去重与合并判断

---

### 11.9 用户替换记忆方案的方式

#### 方式 A：只换底层存储

保留 `DefaultMemoryManager`，实现自己的 `MemoryStoreProtocol`。

#### 方式 B：整层替换

直接继承 `BaseMemoryManager`，完全重写默认策略。

---

### 11.10 用户侧记忆治理边界

Saved Memory 一旦允许自动保存，就必须允许显式治理。

#### 必需能力

* 列出记忆
* 删除记忆
* 钉住 / 取消钉住
* 激活 / 失活
* 清空某用户全部记忆
* 打开 / 关闭记忆功能

#### 暴露边界

* 这些治理接口属于用户显式控制面
* 默认不暴露给 LLM 作为普通工具
* 若产品层希望暴露给模型，必须通过单独的 memory admin 工具集，并要求显式确认
* 子 Agent 默认无权调用治理接口

---

## 十二、上下文层

### 12.1 设计原则

* 上下文层不拥有 Saved Memory 生命周期
* 上下文层消费 `SessionState` 和 `list[MemoryRecord]`
* Session History 与 Saved Memories 分开注入
* 裁剪必须按事务组执行

### 12.2 上下文槽位

```text
Slot 1: System Core
Slot 2: Skill Addon
Slot 3: Saved Memories
Slot 4: Session History
Slot 5: Current Input
```

### 12.3 `ToolTransactionGroup`

#### 字段

* `group_id: str`
* `group_type: Literal["TOOL_BATCH", "SUBAGENT_BATCH", "PLAIN_MESSAGES"]`
* `messages: list[Message]`
* `token_estimate: int`
* `protected: bool`

### 12.4 `ContextSourceProvider`

#### 方法

* `collect_system_core(agent_config, runtime_info) -> str`
* `collect_skill_addon(active_skill) -> str | None`
* `collect_saved_memory_block(records: list[MemoryRecord]) -> str | None`
* `collect_session_groups(session_state: SessionState) -> list[ToolTransactionGroup]`
* `collect_current_input(task_or_prompt) -> Message`

**边界**：

* Saved Memory 的格式化在这里完成
* 记忆层不再返回 `formatted_block`

### 12.5 `ContextBuilder`

#### 方法

* `build_context(system_core, skill_addon, memory_block, session_groups, current_input) -> list[Message]`
* `set_token_budget(max_tokens, reserve_for_output) -> None`
* `_allocate_slot_budgets() -> dict[str, int]`
* `_trim_session_groups(groups, token_limit) -> list[ToolTransactionGroup]`
* `calculate_tokens(messages) -> int`

### 12.6 `ContextCompressor`

#### 策略

* `TOOL_RESULT_SUMMARY`
* `SLIDING_WINDOW`
* `LLM_SUMMARIZE`
* `LLMLINGUA_COMPRESS`

#### 规则

* 先裁剪 session history
* 再压缩长 tool result
* 再考虑对早期历史做总结
* Saved Memories 默认不做有损压缩

### 12.7 `ContextEngineer`

#### 方法

* `prepare_context_for_llm(agent_state, context_materials) -> list[Message]`
* `set_skill_context(skill_prompt: str | None) -> None`
* `build_spawn_seed(session_messages, query, token_budget) -> list[Message]`
* `report_context_stats() -> ContextStats`

---

## 十三、Agent 编排层

### 13.1 `AgentConfig`

* `agent_id: str`
* `model_name: str`
* `system_prompt: str`
* `temperature: float`
* `max_output_tokens: int`
* `max_iterations: int`
* `allow_spawn_children: bool`

### 13.2 `CapabilityPolicy`

* `allowed_tool_categories: list[str] | None`
* `blocked_tool_categories: list[str] | None`
* `allow_network_tools: bool`
* `allow_system_tools: bool`
* `allow_spawn: bool`
* `max_spawn_depth: int`

### 13.3 `BaseAgent`

`BaseAgent` 只聚焦于策略与业务钩子，不承载全部运行依赖。

#### 字段

* `agent_id: str`
* `agent_config: AgentConfig`

#### Hook

* `on_before_run(task, agent_state) -> None`
* `on_iteration_started(iteration_index, agent_state) -> None`
* `on_tool_call_requested(tool_call_request) -> bool`
* `on_tool_call_completed(tool_result) -> None`
* `on_spawn_requested(spawn_spec) -> bool`
* `on_final_answer(answer, agent_state) -> None`
* `should_stop(iteration_result, agent_state) -> bool`

#### 策略方法

* `get_error_policy(error, agent_state) -> ErrorStrategy | None`
* `get_context_policy(agent_state) -> ContextPolicy`
* `get_memory_policy(agent_state) -> MemoryPolicy`
* `get_capability_policy() -> CapabilityPolicy`

---

### 13.4 `Skill`

#### 字段

* `skill_id: str`
* `name: str`
* `description: str`
* `trigger_keywords: list[str]`
* `system_prompt_addon: str`
* `model_override: str | None`
* `temperature_override: float | None`
* `recommended_capability_policy_id: str | None`

### 13.5 Skill Override 生效边界

Skill 的 override 不直接修改 `agent_config`，而是由 `RunCoordinator` 为当前 run 构建一份 `effective config`。

#### 配置分层

* `FrameworkConfig`：进程级默认配置
* `AgentConfig`：Agent 固有配置
* `EffectiveRunConfig`：当前 run 最终生效配置

#### 规则

* override 只在当前 run 生效
* run 结束后自动失效
* 子 Agent 默认不继承，除非 `SubAgentSpec.skill_id` 显式指定
* Skill override 只能覆盖白名单字段：`model_name`、`temperature`
* `max_iterations`、并发上限、子 Agent 配额等运行安全字段默认不可被 Skill 覆盖

### 13.6 `SkillRouter`

#### 方法

* `register_skill(skill)`
* `detect_skill(user_input) -> Skill | None`
* `activate_skill(skill, context_engineer) -> None`
* `deactivate_current_skill() -> None`
* `get_active_skill() -> Skill | None`
* `list_skills() -> list[Skill]`

### 13.7 `ErrorStrategy`

* `RETRY`
* `SKIP`
* `ABORT`

### 13.8 `AgentLoop`

只负责单次 iteration。

#### 方法

* `execute_iteration(agent, deps, agent_state, llm_request) -> IterationResult`
* `_call_llm(deps, request) -> ModelResponse`
* `_check_stop_conditions(agent, model_response, agent_state) -> StopSignal | None`
* `_dispatch_tool_calls(agent, deps, tool_calls, agent_state) -> tuple[list[ToolResult], list[ToolExecutionMeta]]`
* `_handle_iteration_error(agent, error, agent_state) -> ErrorStrategy`

### 13.9 `RunCoordinator`

负责一次 run 的完整生命周期，并独占负责 Skill 激活/去激活与 `SessionState` 写入协调。

#### 方法

* `run(agent, deps, task) -> AgentRunResult`
* `_initialize_state(agent, task) -> AgentState`
* `_build_effective_config(agent, active_skill) -> EffectiveRunConfig`
* `_prepare_llm_request(agent, deps, agent_state) -> LLMRequest`
* `_apply_skill_if_needed(agent, deps, task, agent_state) -> None`
* `_record_iteration(deps, session_state, iteration_result, agent_state) -> None`
* `_deactivate_skill_if_needed(deps) -> None`
* `_finalize_run(agent, agent_state, final_answer, stop_signal) -> AgentRunResult`
* `_handle_run_error(agent, error, agent_state) -> AgentRunResult`

#### 运行流程

1. 初始化 state 与 `SessionState`
2. `memory_manager.begin_session()`
3. 检测并激活 skill
4. 构建 `effective config`
5. while True：

   * 取 Saved Memories
   * 取 Session History
   * 构建 context
   * 执行 iteration
   * 写入 `SessionState`
   * 记录 iteration
   * 如停止则退出
6. `memory_manager.record_turn()`
7. `memory_manager.end_session()`
8. 调用 `_deactivate_skill_if_needed()`
9. 返回 `AgentRunResult`

#### 独占责任

* `RunCoordinator` 负责 Skill 的激活与去激活
* `RunCoordinator` 在正常结束与异常结束路径都必须执行 Skill 清理
* `RunCoordinator` 是 `SessionState` 的唯一写入协调者

---

## 十四、子 Agent 运行时

### 14.1 原则

* 子 Agent 对主 Agent 表现为工具
* 默认隔离、默认最小权限、默认不可递归
* 结果必须摘要化返回
* 子 Agent 不直接写父 store

### 14.2 `SubAgentFactory`

#### 职责

* 合并子 Agent 配置
* 强制 `allow_spawn_children=False`
* 创建 scoped tool registry
* 创建独立运行依赖
* 配置父子记忆边界

### 14.3 `MemoryScope` 调用路径

#### `ISOLATED`

* 子 Agent 使用独立 `MemoryManager + MemoryStore`
* 不读父 Saved Memories
* 不写父 Saved Memories

#### `INHERIT_READ`

* 子 Agent 使用只读 `ParentMemoryView`
* 只读父记忆，不可写回
* 读取的是 spawn 时刻的只读快照，不是实时共享视图

#### `SHARED_WRITE`

* 子 Agent 可以读取父记忆
* 默认读取的也是 spawn 时刻的只读快照
* 子 Agent 不直接写父 store
* 需要写回时，只能调用父 `MemoryManager.remember()`

#### 快照约束

* 子 Agent 运行期间不感知父记忆的后续变化
* 默认不支持 live shared memory
* 这样可保证运行稳定性、可审计性与可重放性

### 14.4 `SubAgentScheduler`

#### 方法

* `submit(spec, factory, parent_agent) -> SubAgentHandle`
* `await_result(handle) -> SubAgentResult`
* `cancel(handle) -> None`
* `get_quota_status(parent_run_id) -> QuotaStatus`
* `_enforce_quota(parent_run_id) -> None`

### 14.5 `SubAgentRuntime`

#### 方法

* `spawn(spec, parent_agent) -> SubAgentResult`
* `get_active_children(parent_run_id) -> list[SubAgentHandle]`
* `cancel_all(parent_run_id) -> int`

### 14.6 `spawn_agent` 内置工具

#### 参数

* `task_input: str`
* `mode: str = "ephemeral"`
* `skill_id: str | None`
* `tool_categories: list[str] | None`
* `memory_scope: str = "isolated"`

#### 返回给 LLM 的内容

只返回 `DelegationSummary`，不返回完整 trace。

---

## 十五、协议层

### 15.1 MCP 客户端 `MCPClientManager`

#### 开源借鉴

* `mcp` 官方 Python SDK

#### 方法

* `connect_server(server_config) -> str`
* `disconnect_server(server_id) -> None`
* `sync_tools_to_catalog(server_id, global_catalog) -> int`
* `call_mcp_tool(server_id, tool_name, arguments) -> Any`
* `load_config_file(path) -> list[MCPServerConfig]`
* `list_connected_servers() -> list[str]`

### 15.2 A2A 客户端 `A2AClientAdapter`

#### 开源借鉴

* `a2a-python` 官方 SDK

#### 方法

* `discover_agent(agent_url) -> AgentCard`
* `delegate_task_to_agent(agent_url, task_input, skill_id=None) -> SubAgentResult`
* `stream_task_to_agent(agent_url, task_input) -> AsyncIterator[TaskEvent]`
* `register_as_a2a_server(agent, host, port) -> None`
* `list_known_agents() -> list[AgentCard]`

---

## 十六、完整数据流

### 16.1 单 Agent 运行流

```text
用户输入
  │
  ▼
RunCoordinator.run(agent, deps, task)
  │
  ├─ begin_session()
  ├─ initialize AgentState
  ├─ create SessionState
  ├─ SkillRouter.detect_skill(task)
  ├─ build effective config
  │
  ├─ [ITERATION LOOP]
  │     ├─ memory_manager.select_for_context()
  │     ├─ collect SessionState messages
  │     ├─ ContextEngineer.prepare_context_for_llm()
  │     ├─ ToolRegistry.export_schemas()
  │     ├─ AgentLoop.execute_iteration()
  │     ├─ write SessionState
  │     ├─ append iteration_history
  │     └─ stop check
  │
  ├─ memory_manager.record_turn()
  ├─ memory_manager.end_session()
  ├─ on_final_answer()
  └─ return AgentRunResult
```

### 16.2 子 Agent 派生流

```text
主 Agent 发起 tool_call: subagent::spawn_agent
  │
  ▼
ToolExecutor._route_execution(source=subagent)
  │
  ▼
DelegationExecutor.delegate_to_subagent(spec, parent_agent)
  │
  ├─ allow_spawn 检查
  ├─ quota 检查
  ├─ build spawn seed
  ├─ SubAgentFactory.create()
  ├─ SubAgentScheduler.submit()
  ├─ await_result()
  └─ DelegationSummary -> ToolResult.output
```

---

## 十七、错误处理

### 17.1 错误分类

#### LLM 错误

* rate limit
* auth
* timeout
* malformed response

#### 工具错误

* not found
* validation error
* execution error
* permission denied
* timeout

#### 子 Agent 错误

* quota exceeded
* timeout
* cancelled
* failed

### 17.2 错误策略

* `RETRY`
* `SKIP`
* `ABORT`

### 17.3 当前版本恢复边界

v2.3 明确声明：

* 不支持进程崩溃后的自动恢复执行
* `AgentState` 与 `SessionState` 是进程内运行态
* 只保证日志、Saved Memories、artifacts 可持久化

---

## 十八、目录结构

```text
agent_framework/
│
├── models/
│   ├── message.py
│   ├── tool.py
│   ├── agent.py
│   ├── session.py
│   ├── memory.py
│   └── subagent.py
│
├── infra/
│   ├── config.py
│   ├── logger.py
│   ├── event_bus.py
│   └── disk_store.py
│
├── adapters/
│   └── model/
│       ├── base_adapter.py
│       └── litellm_adapter.py
│
├── tools/
│   ├── decorator.py
│   ├── catalog.py
│   ├── registry.py
│   ├── executor.py
│   ├── delegation.py
│   ├── confirmation.py
│   └── builtin/
│       ├── filesystem.py
│       ├── system.py
│       └── spawn_agent.py
│
├── memory/
│   ├── base_manager.py
│   ├── store_protocol.py
│   ├── sqlite_store.py
│   ├── default_manager.py
│   └── policies.py
│
├── context/
│   ├── source_provider.py
│   ├── builder.py
│   ├── compressor.py
│   ├── engineer.py
│   └── transaction_group.py
│
├── agent/
│   ├── base_agent.py
│   ├── runtime_deps.py
│   ├── loop.py
│   ├── coordinator.py
│   ├── capability_policy.py
│   ├── skill_router.py
│   └── default_agent.py
│
├── subagent/
│   ├── runtime.py
│   ├── scheduler.py
│   ├── factory.py
│   └── memory_scope.py
│
├── protocols/
│   ├── core.py
│   ├── mcp/
│   │   └── client_manager.py
│   └── a2a/
│       └── client_adapter.py
│
└── examples/
    ├── simple_agent.py
    ├── custom_memory_store.py
    ├── custom_memory_manager.py
    ├── spawn_parallel.py
    ├── mcp_agent.py
    └── a2a_delegation.py
```

---

## 十九、推荐依赖

| 模块       | 依赖                  | 外部服务依赖     | 用途         |
| -------- | ------------------- | ---------- | ---------- |
| LLM 调用   | `litellm`           | 本地模型可离线    | 多模型适配      |
| Token 计数 | `tiktoken`          | 否          | token 统计   |
| 参数校验     | `pydantic v2`       | 否          | schema 与校验 |
| 配置管理     | `pydantic-settings` | 否          | 配置加载       |
| 持久化      | `sqlite3`           | 否          | 默认记忆存储     |
| 上下文压缩    | `llmlingua`         | 否          | 本地语义压缩     |
| 结构化日志    | `structlog`         | 否          | JSON 日志    |
| 事件总线     | `blinker`           | 否          | 解耦通知       |
| CLI      | `click`             | 否          | 命令行接入      |
| MCP      | `mcp` 官方 SDK        | 取决于 server | MCP 协议接入   |
| A2A      | `a2a-python` 官方 SDK | 取决于对端      | A2A 协议接入   |

---

## 二十、安全边界

### 20.1 工具权限

* 高风险工具必须 `require_confirm=True`
* 子 Agent 默认不可访问 `system / network`
* 工具权限由 `CapabilityPolicy` 控制，不由 Skill 控制

### 20.2 子 Agent 递归防护

* `SubAgentFactory` 强制 `allow_spawn_children=False`
* 子 Agent 调 `spawn_agent` 直接返回 `PERMISSION_DENIED`

### 20.3 记忆污染防护

* 默认只保存结构化 Saved Memories
* 不保存推理过程
* 不自动保存工具原始输出
* 子 Agent 不直接写父 store
* 用户可完全替换记忆策略

### 20.4 资源治理

* 子 Agent 总数配额
* 子 Agent 并发配额
* tool batch 并发上限
* deadline 超时取消

---

## 二十一、最小可实现路径

### Phase 1：最小单 Agent 闭环

* Pydantic 模型
* LiteLLMAdapter
* `@tool`
* ToolCatalog / ToolRegistry / ToolExecutor
* SessionState
* ContextBuilder
* AgentLoop + RunCoordinator

### Phase 2：简化记忆层

* `MemoryStoreProtocol`
* `SQLiteMemoryStore`
* `BaseMemoryManager`
* `DefaultMemoryManager`
* Saved Memories 注入上下文

### Phase 3：Skill 与能力策略

* SkillRouter
* CapabilityPolicy
* ScopedToolRegistry
* run-scoped effective config

### Phase 4：子 Agent

* DelegationExecutor
* SubAgentRuntime
* SubAgentFactory
* SubAgentScheduler
* `spawn_agent`

### Phase 5：协议接入

* MCPClientManager
* A2AClientAdapter

---

## 二十二、实现约束总结

1. `RunCoordinator` 负责 run 生命周期，`AgentLoop` 只负责 iteration。
2. `BaseAgent` 负责策略与业务钩子，运行依赖由 `AgentRuntimeDeps` 承载。
3. 除 `RunCoordinator` 外，任何模块不得继续向下传递完整 `AgentRuntimeDeps`。
4. 记忆层默认只做 Saved Memories，不做全文检索。
5. 默认记忆实现只依赖数据库，不引入 `rank_bm25`。
6. `BaseMemoryManager` 与 `MemoryStoreProtocol` 是记忆层主干。
7. 去重与合并逻辑只在 `MemoryManager` 中实现，不进入 store。
8. 记忆层不做 prompt 格式化，格式化由上下文层完成。
9. Session History 必须由 `SessionState` 唯一持有，且由 `RunCoordinator` 协调写入。
10. 工具权限优先级固定为：`CapabilityPolicy` → `ScopedToolRegistry` → `on_tool_call_requested()`。
11. `ContextPolicy` 与 `MemoryPolicy` 属于 run-scoped policy。
12. Skill override 只作用于当前 run 的 `EffectiveRunConfig`，且仅能覆盖白名单字段。
13. 子 Agent 默认不继承父 Skill override。
14. `spawn_agent` 返回给 LLM 的必须是摘要结果。
15. `DelegationExecutor` 是所有委派动作的统一入口。
16. `SHARED_WRITE` 只能通过父 `MemoryManager.remember()` 写回。
17. 子 Agent 读取父记忆默认使用只读快照，而不是实时视图。
18. 默认记忆风格要接近 GPT Saved Memories，而不是知识库检索系统。
19. 用户必须能够列出、删除、启停和钉住自己的 Saved Memories。
20. 记忆治理接口默认不暴露给 LLM。
21. Skill 的激活与去激活必须由 `RunCoordinator` 独占负责。

---

## 二十三、结语

v2.4 的重点不是增加更多功能，而是把主干边界继续收口到可以直接映射代码规范的程度：

* `BaseAgent` 不再兼做依赖容器
* `SessionState` 明确接管会话历史，并由 `RunCoordinator` 独占协调写入
* 记忆层彻底回收为 Saved Memory 治理层
* `ContextPolicy`、`MemoryPolicy`、`EffectiveRunConfig` 等关键契约被补齐
* 工具权限、Skill 生命周期、委派执行边界进一步清晰
* 子 Agent 的父子记忆调用路径与快照语义被正式写死

以这份 v2.4 为基线，可以直接进入代码骨架实现阶段，并且在不破坏主干边界的前提下继续扩展。
---

## 二十四、特定 Agent 实现 (Default / ReAct / Orchestrator)

在 `agent_framework/agent/` 目录下，框架提供了三种内置的具体 Agent 实现，它们都继承自 `BaseAgent`。这不仅展示了如何扩展 `BaseAgent`，也为不同的业务场景提供了开箱即用的选择。

### 24.1 `DefaultAgent`
最基础、最通用的单实体 Agent。
*   **职责**：基于一般的 "系统指令" 进行思考和工具调用，没有特定范式限制。
*   **Prompt 模板**：使用 `DEFAULT_SYSTEM_PROMPT`，强调“如无必要不调用工具”和“每次调用一个工具”。
*   **能力配置**：默认关闭派生能力 (`allow_spawn_children=False`)，专注单线程任务执行。

### 24.2 `ReActAgent`
实现了经典的 ReAct (Reasoning + Acting) 范式的 Agent。
*   **职责**：严格遵循 `Thought -> Action -> Observation -> Final Answer` 循环，适合需要严密逻辑推理的任务。
*   **Prompt 模板**：使用 `REACT_SYSTEM_PROMPT`，强制模型输出特定的思维链格式（在某些实现中可能使用 XML 标签 `<thought>` 等）。
*   **Hook 拦截 (`should_stop`)**：重写了停止判断逻辑，当检测到模型输出了明确的 "Final Answer" 标记时，强制中断迭代循环，无论是否还有剩余的工具调用。

### 24.3 `OrchestratorAgent`
专用于多智能体协作的主控节点 Agent。
*   **职责**：不主要负责具体干活，而是负责将复杂任务拆解，然后委派给特定的子 Agent。收集结果后进行最终汇总。
*   **Prompt 模板**：使用 `ORCHESTRATOR_SYSTEM_PROMPT`，详细指导模型何时该委派、如何并行委派（`spawn_agent`）、如何顺序委派以及内存隔离级别（`memory_scope`）。
*   **能力配置**：默认开启派生能力 (`allow_spawn_children=True`)。
*   **Hook 重写 (`on_spawn_requested`)**：默认允许 (`allowed=True`) 所有的 `spawn_agent` 请求，因为它的核心工作就是派生。

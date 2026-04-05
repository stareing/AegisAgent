# AI Agent Framework — 开发实现文档 v3

> **定位**：从 0 开发、离线优先、边界清晰、默认简单且可替换的 Agent 框架实现文档  
> **版本**：v3  
> **目标**：单 Agent / 工具调用 / Session 管理 / GPT 风格 Saved Memory / 子 Agent / 多协议接入  
> **约束**：完全离线可运行 · 无远端数据库依赖 · 无向量数据库依赖 · 无嵌入 API · 默认记忆仅依赖本地数据库

> **v3 说明**：本版本在 v2.5 主体结构基础上，正式并入已完成代码修复与最终审查结论。若本文后续“v3 正式并入规范”与前文任一旧表述冲突，以 v3 规范为准。

---

## 一、文档目标

本文档用于指导一个 **从 0 开发** 的 AI Agent Framework 实现。目标不是提供概念性架构草图，而是给出一份可以直接开始编码的工程设计文档。

本文档重点解决五类问题：

- 核心运行边界清晰
- 基类、协议、默认实现职责明确
- 默认实现足够简单，离线可运行
- 高级能力可替换，不绑死用户技术路线
- 多人协作实现时行为仍然稳定一致

默认实现语言为 Python 3.11+，核心数据模型基于 `pydantic v2`。

---

## 二、核心设计原则

### 2.1 开源优先
成熟能力直接集成，不重复造轮子。框架自实现范围仅限于：

- 运行时编排
- Agent 循环驱动
- 工具注册与执行胶水层
- 上下文组织与压缩协调
- Saved Memory 抽象与默认实现
- 子 Agent 运行时
- MCP / A2A 协议胶水层

### 2.2 离线优先
默认能力必须在离线环境下可运行：

- 本地模型可用时即可完整运行
- 默认记忆基于本地数据库
- 默认配置从本地文件读取
- 默认日志写本地磁盘

### 2.3 Base / Protocol / Default 三层明确分工
所有可扩展模块按三层定义：

#### `*Protocol`
定义最小契约，用于解耦与类型约束。

#### `Base*`
定义推荐扩展骨架，承载公共逻辑、默认钩子和扩展模板。

#### `Default*`
框架官方默认实现，目标是简单、可解释、可替换。

**约束**：
- 只想兼容框架，可直接实现 `Protocol`
- 想复用默认流程，应继承 `Base*`
- 框架内置实现统一放在 `Default*`

### 2.4 关注点分离
- 编排层只负责运行时调度
- 工具层只负责注册、校验、执行、路由
- 上下文层只负责素材拼装和裁剪
- 记忆层只负责 Saved Memory 的治理
- 协议层只负责与 MCP / A2A 胶合
- 基础设施层只负责配置、日志、事件、磁盘

### 2.5 默认简单，扩展开放
默认实现必须足够简单，不引入过度复杂设计。高级需求通过替换协议实现，而不是把默认主干做成“大而全平台”。

### 2.6 默认安全收敛
- 子 Agent 默认不可递归派生
- 子 Agent 默认不可使用 `system / network` 类工具
- 高风险工具必须显式确认
- Saved Memory 默认只做只读注入，不自动共享写入

### 2.7 显式状态流转
所有运行态都必须通过结构化对象显式传递。禁止通过日志、隐式全局变量或对象副作用传递业务状态。

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

- 用户偏好
- 用户长期约束
- 长期项目背景
- 稳定事实
- 可复用任务提示

### 3.6 Tool Transaction Group
上下文裁剪时不可拆分的消息事务组，至少包括：

- 一条 assistant 的 `tool_calls` 与其对应全部 tool 消息
- assistant 文本与其同条消息中的 tool_calls
- `spawn_agent` 请求与其返回结果

### 3.7 Message Projection
将 `ModelResponse`、`ToolResult`、`DelegationSummary` 等运行对象投影为可进入 `SessionState` 与 LLM 上下文的消息对象的过程。

### 3.8 Effective Run Config
某次 run 中真正生效的配置快照。它由 `FrameworkConfig + AgentConfig + Skill override + Runtime policy` 合成。

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
│         RunStateController / RunPolicyResolver                 │
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

## 五、对象作用域与并发边界

为了避免多 run、子 Agent、并发工具调用之间互相污染，框架中的对象必须声明作用域。

### 5.1 进程级
可跨 run 共享，但不得承载 run 状态：

- `FrameworkConfig`
- `GlobalToolCatalog`
- `StructLogger`
- `EventBus`
- Skill 定义表

### 5.2 Agent 级
描述某个 Agent 的固有能力，但不承载某次运行态：

- `AgentConfig`
- `BaseAgent`
- 默认 `CapabilityPolicy` 模板

### 5.3 Run 级
一次 run 独占，不得跨 run 复用：

- `AgentState`
- `SessionState`
- `EffectiveRunConfig`
- 当前 run 的 `ScopedToolRegistry`
- 当前 run 的上下文统计

### 5.4 SubAgent Run 级
子 Agent 独占，不得和父 run 或其他子 run 共用：

- 子 `AgentState`
- 子 `SessionState`
- 子 `EffectiveRunConfig`
- 子 run 的 tool scope / memory view

### 5.5 并发约束
- 多个 run 不得共享 `SessionState`
- 多个 run 不得共享 `AgentState`
- 子 Agent 不得共享父 run 的可写 `MemoryManager`
- `SkillRouter` 不得保存 active skill 运行态
- 任何 run-scoped 对象都不得挂在进程级单例中

---

## 六、v2.5 边界修订清单（正式并入文档）

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
修订：Skill override 只在当前 run 的 `EffectiveRunConfig` 中生效，不直接修改 `agent_config`，子 Agent 默认不继承。

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

### 缺陷 19：`Message.metadata` 可能越界进入 LLM 上下文
修订：引入 `InternalMessage` / `LLMMessage` 语义边界；送给模型前必须进行 message sanitization，`metadata` 默认不进入模型上下文。

### 缺陷 20：`ToolMeta.parameters_schema` 运行中可变会导致契约漂移
修订：工具注册完成后 schema 冻结；运行时只能做可见性裁剪，不能修改 schema 契约。

### 缺陷 21：记忆自动提取触发时机不明确
修订：自动记忆提取只允许在 `record_turn()` 中发生，并且每个 turn 最多触发一次。

### 缺陷 22：`MemoryCandidate` 缺少来源类型与置信度
修订：补充 `source_type` 与 `confidence`，区分用户显式表述、模型推断、工具导出等来源，避免低置信度内容污染记忆。

### 缺陷 23：Saved Memory 格式化缺少确定性约束
修订：相同记忆集合必须生成稳定输出，展示顺序、模板、role 和槽位必须固定。

### 缺陷 24：`iteration_history` 语义不明
修订：它是运行内不可改写的结构化审计轨迹；失败、skip、retry 也必须入历史，且不能被压缩流程改写。

### 缺陷 25：`ToolResult.output` 边界过宽
修订：要求 output 必须是可 JSON 序列化 DTO；大对象必须先摘要化；注入上下文的永远是 message-safe projection。

### 缺陷 26：日志有被滥用为业务状态通道的风险
修订：日志只服务于观测与审计，业务判断不得依赖日志。

### 缺陷 27：`Artifact` 生命周期与摘要边界不明
修订：artifact 只表示可引用结果产物；memory 层只允许吸收摘要，不吸收 artifact 本体。

### 缺陷 28：确认逻辑的决策者与执行者未分离
修订：是否需要确认由 policy 与 tool meta 决定；`ConfirmationHandler` 只负责执行确认流程。

### 缺陷 29：`ScopedToolRegistry` 容易被误解为安全边界
修订：它只负责可见性裁剪；真正执行权限边界在 `ToolExecutor + CapabilityPolicy`。

### 缺陷 30：A2A 与本地 subagent 委派失败语义未统一
修订：`DelegationExecutor` 必须把本地与远端委派结果统一映射成标准 `SubAgentResult` 与标准错误码。

### 缺陷 31：资源限制未区分硬限制与软限制
修订：补充配额语义；硬限制必须拒绝或停止，软限制允许降级处理。

### 缺陷 32：接口中 `None` 的语义未统一
修订：`None` 只表示“语义上不存在”，失败必须通过错误对象或错误码表达，空集合必须返回空列表。

---

## 七、基础设施层

### 7.1 配置管理 `Config`

#### 开源借鉴
- `pydantic-settings`

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
- `default_model_name`
- `temperature`
- `max_output_tokens`
- `api_base`
- `timeout_ms`
- `max_retries`

#### `ContextConfig`
- `max_context_tokens`
- `reserve_for_output`
- `compress_threshold_ratio`
- `default_compression_strategy`
- `spawn_seed_ratio`

#### `MemoryConfig`
- `db_path`
- `enable_saved_memory`
- `auto_extract_memory`
- `max_memories_in_context`
- `max_memory_items_per_user`
- `allow_user_memory_namespace`
- `allow_memory_management_api`

#### `ToolConfig`
- `confirmation_handler_type`
- `max_concurrent_tool_calls`
- `allow_parallel_tool_calls`

#### `SubAgentConfig`
- `max_sub_agents_per_run`
- `max_concurrent_sub_agents`
- `per_sub_agent_max_tokens`
- `default_deadline_ms`
- `default_max_iterations`
- `allow_recursive_spawn`

#### `LoggingConfig`
- `log_dir`
- `json_output`
- `level`

#### 方法
- `load_config(config_path) -> FrameworkConfig`
- `reload_config() -> None`

---

### 7.2 结构化日志 `StructLogger`

#### 开源借鉴
- `structlog`

#### 标准字段
- `timestamp`
- `level`
- `run_id`
- `parent_run_id`
- `spawn_id`
- `iteration_index`
- `event`
- `duration_ms`
- `error_code`

#### 标准事件
- `run.started`
- `run.finished`
- `run.failed`
- `iteration.started`
- `iteration.completed`
- `llm.called`
- `llm.responded`
- `tool.dispatched`
- `tool.completed`
- `tool.failed`
- `context.compressed`
- `memory.saved`
- `memory.updated`
- `memory.deleted`
- `subagent.spawned`
- `subagent.completed`
- `subagent.failed`

#### 日志边界
- 日志只服务于观测、排障、审计
- 任何业务判断不得依赖日志内容
- 运行态信息必须通过结构化对象显式传递
- 日志缺失不能影响正确性

---

### 7.3 事件总线 `EventBus`

#### 开源借鉴
- `blinker`

#### 方法
- `subscribe(event_name, handler)`
- `publish(event_name, payload)`
- `unsubscribe(event_name, handler)`

---

### 7.4 本地磁盘存储 `DiskStore`

#### 方法
- `write_json(path, data)`
- `read_json(path)`
- `write_text(path, text)`
- `read_text(path)`
- `ensure_directory(path)`
- `list_files(directory, pattern)`
- `atomic_write(path, content)`

---

## 八、数据模型层

所有模型使用 `pydantic v2`。

### 8.1 模型分层

#### Domain Models
框架内部业务对象。

#### DTO Models
跨进程、跨协议、API 暴露对象。

#### Persistence Models
数据库或文件持久化对象。

---

### 8.2 消息与模型响应

#### `InternalMessage`
框架内部消息对象，可携带内部元数据。

- `role: Literal["system", "user", "assistant", "tool"]`
- `content: str | None`
- `tool_calls: list[ToolCallRequest] | None`
- `tool_call_id: str | None`
- `name: str | None`
- `metadata: dict | None`

#### `LLMMessage`
送给模型的安全消息对象。

- `role: Literal["system", "user", "assistant", "tool"]`
- `content: str | None`
- `tool_calls: list[ToolCallRequest] | None`
- `tool_call_id: str | None`
- `name: str | None`

#### Message Sanitization 规则
- `metadata` 默认不进入 LLM 上下文
- trace、权限、UI、原始 SDK 返回、内部异常等都不得进入 `LLMMessage`
- 送给模型的消息必须先做 message-safe projection

#### `ToolCallRequest`
- `id: str`
- `function_name: str`
- `arguments: dict`

#### `TokenUsage`
- `prompt_tokens: int`
- `completion_tokens: int`
- `total_tokens: int`

#### `ModelResponse`
- `content: str | None`
- `tool_calls: list[ToolCallRequest]`
- `finish_reason: Literal["stop", "tool_calls", "length", "error"]`
- `usage: TokenUsage`
- `raw_response_meta: dict | None`

#### `LLMResponseChunk`
流式返回的最小消息块。

- `delta_content: str | None`
- `delta_tool_calls: list[ToolCallRequest] | None`
- `finish_reason: str | None`

---

### 8.3 工具相关模型

#### `ToolMeta`
- `name: str`
- `description: str`
- `parameters_schema: dict`
- `category: str`
- `require_confirm: bool`
- `is_async: bool`
- `tags: list[str]`
- `source: Literal["local", "mcp", "a2a", "subagent"]`
- `namespace: str | None`
- `mcp_server_id: str | None`
- `a2a_agent_url: str | None`

#### Schema 冻结约束
- `parameters_schema` 在注册完成后视为只读
- 运行时只允许做可见性裁剪
- 不允许在 run 中直接修改既有工具契约

#### `ToolEntry`
- `meta: ToolMeta`
- `callable_ref: Callable | None`
- `validator_model: type[BaseModel] | None`

#### `FieldError`
- `field: str`
- `expected: str | None`
- `received: str | None`
- `message: str`

#### `ToolExecutionError`
- `error_type: Literal["VALIDATION_ERROR", "EXECUTION_ERROR", "PERMISSION_DENIED", "NOT_FOUND", "TIMEOUT", "QUOTA_EXCEEDED", "INTERNAL_ERROR"]`
- `error_code: str`
- `message: str`
- `field_errors: list[FieldError] | None`
- `retryable: bool`

#### `ToolResult`
- `tool_call_id: str`
- `tool_name: str`
- `success: bool`
- `output: dict | list | str | int | float | bool | None`
- `error: ToolExecutionError | None`

#### `ToolResult.output` 边界
- 必须可 JSON 序列化
- 不允许直接塞 callable、连接对象、原始 SDK client、exception object
- 大对象必须先被摘要化或转为 artifact 引用
- 注入上下文时必须先投影为 message-safe 内容

#### `ToolExecutionMeta`
- `execution_time_ms: int`
- `source: Literal["local", "mcp", "a2a", "subagent"]`
- `trace_ref: str | None`
- `retry_count: int`

---

### 8.4 运行态模型

#### `AgentStatus`
- `IDLE`
- `RUNNING`
- `TOOL_CALLING`
- `SPAWNING`
- `FINISHED`
- `ERROR`
- `PAUSED`

#### `StopReason`
- `LLM_STOP`
- `MAX_ITERATIONS`
- `USER_CANCEL`
- `CUSTOM`
- `ERROR`
- `OUTPUT_TRUNCATED`

#### `StopSignal`
- `reason: StopReason`
- `message: str | None`

#### `IterationError`
- `error_type: str`
- `error_code: str`
- `error_message: str`
- `retryable: bool`
- `stacktrace: str | None`

#### `IterationResult`
- `iteration_index: int`
- `model_response: ModelResponse | None`
- `tool_results: list[ToolResult]`
- `tool_execution_meta: list[ToolExecutionMeta]`
- `stop_signal: StopSignal | None`
- `error: IterationError | None`

#### `iteration_history` 语义
- 属于运行内不可改写的结构化审计轨迹
- 每次 iteration，无论成功、失败、skip、retry，都必须入历史
- retry 会产生新条目，不覆盖旧条目
- 上下文压缩不得改写 `iteration_history`

#### `AgentState`
- `run_id: str`
- `task: str`
- `status: AgentStatus`
- `iteration_count: int`
- `turn_count: int`
- `total_tokens_used: int`
- `active_skill_id: str | None`
- `spawn_count: int`
- `iteration_history: list[IterationResult]`

#### `AgentRunResult`
- `run_id: str`
- `success: bool`
- `final_answer: str | None`
- `stop_signal: StopSignal`
- `usage: TokenUsage`
- `iterations_used: int`
- `artifacts: list[Artifact]`
- `error: str | None`

#### `ContextPolicy`
run-scoped 上下文策略对象。

- `allow_compression: bool`
- `prefer_recent_history: bool`
- `max_session_groups: int | None`
- `force_include_saved_memory: bool`

#### `MemoryPolicy`
run-scoped 记忆策略对象。

- `memory_enabled: bool`
- `auto_extract: bool`
- `allow_overwrite_pinned: bool`
- `allow_auto_save_from_tools: bool`

#### Policy 解释边界
- `ContextPolicy` 只能由 `ContextEngineer` 解释
- `MemoryPolicy` 只能由 `MemoryManager` 解释
- `RunCoordinator` 只负责传递 policy，不解释内部字段

#### `EffectiveRunConfig`
当前 run 的最终生效配置。

- `model_name: str`
- `temperature: float`
- `max_output_tokens: int`
- `max_iterations: int`
- `reserve_for_output: int`
- `max_concurrent_tool_calls: int`
- `subagent_token_budget: int`
- `allow_parallel_tool_calls: bool`

#### `EffectiveRunConfig` 约束
- run 开始后视为只读
- 不是状态对象，不记录运行统计
- 只能由 `RunCoordinator` 构建
- 其他模块不得修改

---

### 8.5 Session 相关模型

#### `SessionState`
当前 run 的会话状态唯一持有者。

- `session_id: str`
- `run_id: str`
- `messages: list[InternalMessage]`
- `started_at: datetime`
- `last_updated_at: datetime`

#### `SessionState` 只负责
- 追加 user/assistant/tool 消息
- 返回消息历史
- 为上下文层提供消息源

#### `SessionState` 不负责
- Saved Memory
- 长期持久化
- 检索
- 提示词格式化

#### 写入责任边界
- `RunCoordinator` 是 `SessionState` 的唯一写入协调者
- `AgentLoop` 只返回 `IterationResult`
- `ToolExecutor` 不直接写会话消息
- `RunCoordinator._record_iteration()` 统一将 `ModelResponse` 与 `ToolResult` 投影为消息并写入 `SessionState`

#### 消息投影规则
- assistant 带 `tool_calls` 时，保留一条 assistant message
- 每个 `ToolResult` 投影为一条 tool message
- tool error 也必须投影为 tool message
- subagent 结果只投影 `DelegationSummary`
- 投影顺序固定为：assistant -> tool1 -> tool2 -> ...
- `batch_execute()` 即使并发执行，也必须按输入顺序返回，确保投影顺序稳定

---

### 8.6 记忆相关模型

#### `MemoryKind`
- `USER_PROFILE`
- `USER_PREFERENCE`
- `USER_CONSTRAINT`
- `PROJECT_CONTEXT`
- `TASK_HINT`
- `CUSTOM`

#### `MemorySourceType`
- `explicit_user`
- `inferred`
- `tool_derived`
- `admin`

#### `MemoryConfidence`
- `high`
- `medium`
- `low`

#### `MemoryRecord`
默认 Saved Memory 条目。

- `memory_id: str`
- `user_id: str | None`
- `agent_id: str`
- `kind: MemoryKind`
- `title: str`
- `content: str`
- `tags: list[str]`
- `is_active: bool`
- `is_pinned: bool`
- `source: str | None`
- `created_at: datetime`
- `updated_at: datetime`
- `last_used_at: datetime | None`
- `use_count: int`
- `version: int`
- `extra: dict | None`

#### `MemoryCandidate`
- `kind: MemoryKind`
- `title: str`
- `content: str`
- `tags: list[str]`
- `reason: str | None`
- `source_type: MemorySourceType`
- `confidence: MemoryConfidence`

#### 默认写入规则
- `explicit_user + high` 优先写入
- `inferred` 默认更保守
- `tool_derived` 只有在结构化、明确、低歧义时才允许写入

#### `MemoryUpdateAction`
- `UPSERT`
- `DELETE`
- `IGNORE`

#### `RememberSourceContext`
用于说明记忆写入来源。

- `source_type: Literal["user", "agent", "subagent", "admin"]`
- `source_run_id: str | None`
- `source_spawn_id: str | None`

---

### 8.7 子 Agent 模型

#### `SpawnMode`
- `EPHEMERAL`
- `FORK`
- `LONG_LIVED`

#### `MemoryScope`
- `ISOLATED`
- `INHERIT_READ`
- `SHARED_WRITE`

#### `SubAgentSpec`
- `parent_run_id: str`
- `spawn_id: str`
- `mode: SpawnMode`
- `task_input: str`
- `agent_config_override: dict`
- `skill_id: str | None`
- `tool_category_whitelist: list[str] | None`
- `context_seed: list[LLMMessage] | None`
- `memory_scope: MemoryScope`
- `token_budget: int`
- `max_iterations: int`
- `deadline_ms: int`
- `allow_spawn_children: bool`

#### `Artifact`
- `artifact_type: str`
- `name: str`
- `uri: str | None`
- `content: dict | str | None`
- `metadata: dict | None`

#### `Artifact` 边界
- 只表示可引用的结果产物描述
- 小对象可放在 `content`
- 大对象必须通过 `uri` 或文件路径引用
- 生命周期由产出方 runtime 管理
- memory 层只允许吸收 artifact 摘要，不吸收本体

#### `SubAgentHandle`
- `sub_agent_id: str`
- `spawn_id: str`
- `parent_run_id: str`
- `status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED", "TIMEOUT", "CANCELLED"]`
- `created_at: datetime`

#### `SubAgentResult`
- `spawn_id: str`
- `success: bool`
- `final_answer: str | None`
- `error: str | None`
- `artifacts: list[Artifact]`
- `usage: TokenUsage`
- `iterations_used: int`
- `duration_ms: int`
- `trace_ref: str | None`
- `error_code: str | None`

#### `DelegationSummary`
- `status: str`
- `summary: str`
- `artifacts_digest: list[str]`
- `error_code: str | None`

---

## 九、错误码注册表与 None 语义

### 9.1 通用错误码
- `TIMEOUT`
- `PERMISSION_DENIED`
- `NOT_FOUND`
- `VALIDATION_ERROR`
- `INTERNAL_ERROR`

### 9.2 工具错误码
- `INVALID_ARGUMENT_TYPE`
- `TOOL_EXECUTION_FAILED`

### 9.3 委派错误码
- `QUOTA_EXCEEDED`
- `REMOTE_UNAVAILABLE`
- `DELEGATION_FAILED`

### 9.4 注册表规则
- 新错误码必须向注册表追加
- 给模型看的错误码必须来自注册表
- 不允许自由命名错误码
- 不允许直接透出原始异常文本作为 error code

### 9.5 None 语义规范
- `None` 只表示“语义上不存在”
- “失败”必须通过 error object 或 error code 表达
- “空集合”必须返回空列表，不返回 `None`
- “尚未生成”仅限内部运行态，不应进入最终 DTO

---

## 十、Protocol 与基类设计

### 10.1 运行依赖容器 `AgentRuntimeDeps`

#### 字段
- `tool_registry: ToolRegistryProtocol`
- `tool_executor: ToolExecutorProtocol`
- `memory_manager: MemoryManagerProtocol`
- `context_engineer: ContextEngineerProtocol`
- `model_adapter: ModelAdapterProtocol`
- `skill_router: SkillRouterProtocol`
- `confirmation_handler: ConfirmationHandlerProtocol`
- `sub_agent_runtime: SubAgentRuntimeProtocol | None`
- `delegation_executor: DelegationExecutorProtocol | None`

#### 依赖传递约束
- `RunCoordinator` 可以持有完整 `AgentRuntimeDeps`
- 其他模块不得继续向下传递完整 `AgentRuntimeDeps`
- 下层对象只接收自身最小必需依赖
- 禁止通过 `deps` 形成跨层随意调用

---

### 10.2 模型协议 `ModelAdapterProtocol`

#### 方法
- `complete(messages, tools, temperature, max_tokens) -> ModelResponse`
- `stream_complete(messages, tools) -> AsyncIterator[LLMResponseChunk]`
- `count_tokens(messages) -> int`
- `supports_parallel_tool_calls() -> bool`

---

### 10.3 工具注册协议 `ToolRegistryProtocol`

#### 方法
- `get_tool(name) -> ToolEntry`
- `has_tool(name) -> bool`
- `list_tools(category=None, tags=None, source=None) -> list[ToolEntry]`
- `export_schemas(whitelist=None) -> list[dict]`

#### `ScopedToolRegistry` 边界
- 只负责可见性裁剪
- 不负责安全执行权限
- 即使工具不可见，执行时仍必须再次检查 `CapabilityPolicy`

---

### 10.4 工具执行协议 `ToolExecutorProtocol`

#### 方法
- `execute(tool_call_request) -> tuple[ToolResult, ToolExecutionMeta]`
- `batch_execute(tool_call_requests) -> list[tuple[ToolResult, ToolExecutionMeta]]`

#### 顺序约束
- `batch_execute()` 即使内部并发执行，返回结果也必须与输入顺序一致

---

### 10.5 委派执行协议 `DelegationExecutorProtocol`

#### 边界
- `DelegationExecutor` 是所有委派动作的统一入口
- `ToolExecutor` 不直接处理子 Agent 生命周期或 A2A 远端生命周期
- `SubAgentRuntime` 只负责本地子 Agent 生命周期
- `A2AClientAdapter` 只负责远端 Agent 协议调用

#### 方法
- `delegate_to_subagent(spec: SubAgentSpec, parent_agent: BaseAgent) -> SubAgentResult`
- `delegate_to_a2a(agent_url: str, task_input: str, skill_id: str | None = None) -> SubAgentResult`

#### 失败语义统一
`DelegationExecutor` 必须将本地 subagent 与 A2A 委派结果统一映射为标准 `SubAgentResult` 与标准错误码：
- `TIMEOUT`
- `QUOTA_EXCEEDED`
- `PERMISSION_DENIED`
- `DELEGATION_FAILED`
- `REMOTE_UNAVAILABLE`

---

### 10.6 记忆存储协议 `MemoryStoreProtocol`

#### 方法
- `save(record: MemoryRecord) -> str`
- `update(record: MemoryRecord) -> None`
- `delete(memory_id: str) -> None`
- `get(memory_id: str) -> MemoryRecord | None`
- `list_by_user(agent_id: str, user_id: str | None, active_only: bool = True) -> list[MemoryRecord]`
- `list_by_kind(agent_id: str, user_id: str | None, kind: MemoryKind) -> list[MemoryRecord]`
- `list_recent(agent_id: str, user_id: str | None, limit: int) -> list[MemoryRecord]`
- `touch(memory_id: str) -> None`
- `count(agent_id: str, user_id: str | None) -> int`

#### 存储边界
- 只负责数据库读写
- 不负责去重、相似判断、上下文格式化
- 不负责记忆治理策略解释

---

### 10.7 记忆管理协议 `MemoryManagerProtocol`

#### 方法
- `begin_session(run_id: str, agent_id: str, user_id: str | None) -> None`
- `select_for_context(task: str, agent_state: AgentState) -> list[MemoryRecord]`
- `record_turn(user_input: str, final_answer: str | None, iteration_results: list[IterationResult]) -> None`
- `remember(candidate: MemoryCandidate, source_context: RememberSourceContext | None = None) -> str | None`
- `forget(memory_id: str) -> None`
- `list_memories(agent_id: str, user_id: str | None) -> list[MemoryRecord]`
- `pin(memory_id: str) -> None`
- `unpin(memory_id: str) -> None`
- `activate(memory_id: str) -> None`
- `deactivate(memory_id: str) -> None`
- `clear_memories(agent_id: str, user_id: str | None) -> int`
- `set_enabled(enabled: bool) -> None`
- `end_session() -> None`

#### 自动提取边界
- `record_turn()` 是唯一自动提取入口
- 一个 conversation turn 最多触发一次自动提取
- iteration 中途不做自动提取
- tool 执行中的中间状态不得直接进入自动提取流程

#### 记忆治理暴露边界
- 记忆治理接口属于用户显式控制面
- 默认不暴露给 LLM
- 如需暴露，只能通过单独的 `memory_admin::*` 工具集
- 该工具集必须显式确认
- 子 Agent 默认无权调用治理接口

---

### 10.8 上下文协议 `ContextEngineerProtocol`

#### 方法
- `prepare_context_for_llm(agent_state, context_materials) -> list[LLMMessage]`
- `set_skill_context(skill_prompt: str | None) -> None`
- `build_spawn_seed(session_messages, query, token_budget) -> list[LLMMessage]`
- `report_context_stats() -> ContextStats`

#### 只读约束
- `ContextEngineer` 是只读消费者
- 不允许修改 `SessionState`
- 不允许修改 `MemoryRecord`
- 不允许修改 `AgentState`
- 压缩结果只影响本次请求，不回写源状态

---

### 10.9 子 Agent 运行时协议 `SubAgentRuntimeProtocol`

#### 方法
- `spawn(spec: SubAgentSpec, parent_agent: BaseAgent) -> SubAgentResult`
- `get_active_children(parent_run_id: str) -> list[SubAgentHandle]`
- `cancel_all(parent_run_id: str) -> int`

---

## 十一、模型适配层

### 11.1 开源借鉴
- `litellm`
- `tiktoken`

### 11.2 `ModelAdapter` 抽象基类

#### 方法
- `complete(messages, tools, temperature, max_tokens) -> ModelResponse`
- `stream_complete(messages, tools) -> AsyncIterator[LLMResponseChunk]`
- `count_tokens(messages) -> int`
- `supports_parallel_tool_calls() -> bool`

### 11.3 `LiteLLMAdapter`

#### 规则
- 指数退避重试
- 统一异常映射
- tool arguments JSON 解析失败时不抛出，只返回空 dict 并告警

#### 异常类型
- `LLMCallError`
- `LLMRateLimitError`
- `LLMAuthError`
- `LLMTimeoutError`

---

## 十二、工具层

### 12.1 `@tool` 装饰器

#### 行为
1. 读取 docstring 第一段作为 description
2. 根据函数签名生成临时 `pydantic` 参数模型
3. 调用 `model_json_schema()` 生成 JSON Schema
4. 自动识别 async
5. 在函数上挂 `__tool_meta__`

#### 可选参数
- `name`
- `description`
- `category`
- `require_confirm`
- `tags`
- `namespace`

---

### 12.2 工具目录与注册表

#### `GlobalToolCatalog`
进程级工具总目录，不直接给 Agent 使用。

#### `ToolRegistry`
某个运行实例的工具快照视图。

#### `ScopedToolRegistry`
只读白名单视图，用于在当前 run 中裁剪可见工具集。

---

### 12.3 命名策略

内部工具名统一采用：

- 本地：`local::<name>`
- MCP：`mcp::<server_id>::<name>`
- A2A：`a2a::<agent_alias>::<name>`
- 子 Agent：`subagent::spawn_agent`

---

### 12.4 权限边界优先级

工具权限统一按以下优先级生效：

1. `CapabilityPolicy`：定义能力上界
2. `ScopedToolRegistry`：定义当前 run 可见工具集
3. `on_tool_call_requested()`：定义最终运行时拦截

#### 安全执行双重检查
- 导出 schema 时先做可见性过滤
- 真正执行时 `ToolExecutor` 仍必须再次检查 `CapabilityPolicy`
- 可见性过滤不是安全边界，执行校验才是

---

### 12.5 确认边界

#### 确认决策优先级
1. deployment / `CapabilityPolicy` 可强制升级确认
2. `ToolMeta.require_confirm=True` 时必须确认
3. `ConfirmationHandler` 只负责执行确认流程，不负责决定是否需要确认

---

### 12.6 `ToolExecutor`

#### 方法
- `execute(tool_call_request) -> tuple[ToolResult, ToolExecutionMeta]`
- `batch_execute(tool_call_requests) -> list[tuple[ToolResult, ToolExecutionMeta]]`
- `_validate_arguments(tool_entry, arguments) -> dict | ToolExecutionError`
- `_route_execution(tool_entry, validated_arguments) -> Any`
- `_handle_tool_error(tool_name, error) -> tuple[ToolResult, ToolExecutionMeta]`

#### 路由规则
- `local` -> 本地函数
- `mcp` -> `MCPClientManager.call_mcp_tool()`
- `a2a` -> `DelegationExecutor.delegate_to_a2a()`
- `subagent` -> `DelegationExecutor.delegate_to_subagent()`

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

## 十三、记忆层（简化版，可插件化）

### 13.1 设计目标

默认记忆只解决三类问题：

1. 记住用户稳定偏好
2. 记住长期约束和项目背景
3. 在后续对话中把这些信息简洁注入上下文

默认不做：
- 全文检索
- 排序召回流水线
- embedding 检索
- 自动存全部聊天
- session history 数据库化

---

### 13.2 GPT 风格 Saved Memory 原则

#### 应保存
- 用户偏好的输出方式
- 语言偏好、称呼偏好
- 长期项目背景
- 稳定约束
- 高复用任务提示

#### 不应保存
- 一次性临时问题
- 完整聊天记录
- 工具原始输出
- 中间推理过程
- 低置信度猜测

---

### 13.3 默认数据库表结构

默认实现使用 SQLite。

#### 表 `saved_memories`
- `memory_id TEXT PRIMARY KEY`
- `agent_id TEXT NOT NULL`
- `user_id TEXT NULL`
- `kind TEXT NOT NULL`
- `title TEXT NOT NULL`
- `content TEXT NOT NULL`
- `tags TEXT NOT NULL`
- `is_active INTEGER NOT NULL`
- `is_pinned INTEGER NOT NULL`
- `source TEXT NULL`
- `created_at TEXT NOT NULL`
- `updated_at TEXT NOT NULL`
- `last_used_at TEXT NULL`
- `use_count INTEGER NOT NULL`
- `version INTEGER NOT NULL`
- `extra TEXT NULL`

#### 索引建议
- `(agent_id, user_id, is_active)`
- `(agent_id, user_id, kind)`
- `(agent_id, user_id, updated_at DESC)`

---

### 13.4 `BaseMemoryManager`

#### 责任
- 判断什么值得记住
- 决定是否更新、覆盖或忽略记忆
- 决定哪些记忆进入当前上下文
- 提供用户治理接口

#### 不负责
- prompt 格式化
- session history 管理
- 模型调用
- 工具执行

#### 方法
- `begin_session(run_id, agent_id, user_id) -> None`
- `select_for_context(task, agent_state) -> list[MemoryRecord]`
- `record_turn(user_input, final_answer, iteration_results) -> None`
- `extract_candidates(user_input, final_answer, iteration_results) -> list[MemoryCandidate]`
- `merge_candidate(candidate, existing_records) -> MemoryUpdateAction`
- `remember(candidate, source_context=None) -> str | None`
- `forget(memory_id) -> None`
- `pin(memory_id) -> None`
- `unpin(memory_id) -> None`
- `activate(memory_id) -> None`
- `deactivate(memory_id) -> None`
- `clear_memories(agent_id, user_id) -> int`
- `set_enabled(enabled: bool) -> None`
- `end_session() -> None`

---

### 13.5 `DefaultMemoryManager`

#### 默认行为
1. 每个 turn 结束后检查是否出现可保存信息
2. 提取少量 `MemoryCandidate`
3. 从 store 读取候选合并范围
4. 执行去重/更新/忽略
5. 下次上下文构建时，按简单规则挑选少量记忆注入

#### 默认提取规则
优先提取：
- “以后都用中文回答我”
- “我正在做一个离线 Agent 框架”
- “不要使用向量数据库”
- “我更关注 base class 和 protocol”

默认忽略：
- 临时问法
- 闲聊
- 工具输出细节

---

### 13.6 去重与合并策略

不使用复杂检索，仅使用简单规则：

1. 同 `kind + 归一化 title` 命中已有条目时，进入更新逻辑
2. 同 `content` 完全一致时忽略
3. `is_pinned=True` 的条目不自动覆盖
4. 新内容与旧内容冲突时，提升 `version`

**约束**：
- 去重逻辑只在 `MemoryManager` 中实现
- `MemoryStoreProtocol` 不做“相似判断”

---

### 13.7 上下文注入选择规则

不做全文搜索，默认只按以下规则选择：

1. `is_pinned=True` 的记忆优先
2. 与当前 task 关键词显式匹配的记忆
3. 最近更新的活跃记忆
4. 总数量限制在 `max_memories_in_context`

**边界**：
- 记忆层只返回 `list[MemoryRecord]`
- 记忆的文本格式化由上下文层负责

---

### 13.8 `MemoryStoreProtocol` 的默认实现：`SQLiteMemoryStore`

#### 方法
- `save(record) -> str`
- `update(record) -> None`
- `delete(memory_id) -> None`
- `get(memory_id) -> MemoryRecord | None`
- `list_by_user(agent_id, user_id, active_only=True) -> list[MemoryRecord]`
- `list_by_kind(agent_id, user_id, kind) -> list[MemoryRecord]`
- `list_recent(agent_id, user_id, limit) -> list[MemoryRecord]`
- `touch(memory_id) -> None`
- `count(agent_id, user_id) -> int`

#### 设计要求
- 只负责数据库读写
- 不负责候选提取
- 不负责上下文格式化
- 不负责去重与合并判断

---

### 13.9 用户替换记忆方案的方式

#### 方式 A：只换底层存储
保留 `DefaultMemoryManager`，实现自己的 `MemoryStoreProtocol`。

#### 方式 B：整层替换
直接继承 `BaseMemoryManager`，完全重写默认策略。

---

### 13.10 用户侧记忆治理边界

#### 必需能力
- 列出记忆
- 删除记忆
- 钉住 / 取消钉住
- 激活 / 失活
- 清空某用户全部记忆
- 打开 / 关闭记忆功能

#### 暴露边界
- 属于用户显式控制面
- 默认不暴露给 LLM
- 若需要暴露，必须通过单独的 `memory_admin::*` 工具集
- 必须要求显式确认
- 子 Agent 默认无权调用治理接口

---

## 十四、上下文层

### 14.1 设计原则
- 上下文层不拥有 Saved Memory 生命周期
- 上下文层消费 `SessionState` 和 `list[MemoryRecord]`
- Session History 与 Saved Memories 分开注入
- 裁剪必须按事务组执行
- 格式化必须是确定性的

### 14.2 上下文槽位

```text
Slot 1: System Core
Slot 2: Skill Addon
Slot 3: Saved Memories
Slot 4: Session History
Slot 5: Current Input
```

### 14.3 `ToolTransactionGroup`

#### 字段
- `group_id: str`
- `group_type: Literal["TOOL_BATCH", "SUBAGENT_BATCH", "PLAIN_MESSAGES"]`
- `messages: list[LLMMessage]`
- `token_estimate: int`
- `protected: bool`

### 14.4 `ContextSourceProvider`

#### 方法
- `collect_system_core(agent_config, runtime_info) -> str`
- `collect_skill_addon(active_skill) -> str | None`
- `collect_saved_memory_block(records: list[MemoryRecord]) -> str | None`
- `collect_session_groups(session_state: SessionState) -> list[ToolTransactionGroup]`
- `collect_current_input(task_or_prompt) -> LLMMessage`

#### 格式化稳定性约束
- 相同输入必须生成稳定输出
- 记忆展示顺序必须有固定规则
- 同一类记忆使用固定模板
- `Saved Memories` 块统一插入同一槽位、同一 role

### 14.5 `ContextBuilder`

#### 方法
- `build_context(system_core, skill_addon, memory_block, session_groups, current_input) -> list[LLMMessage]`
- `set_token_budget(max_tokens, reserve_for_output) -> None`
- `_allocate_slot_budgets() -> dict[str, int]`
- `_trim_session_groups(groups, token_limit) -> list[ToolTransactionGroup]`
- `calculate_tokens(messages) -> int`

### 14.6 `ContextCompressor`

#### 策略
- `TOOL_RESULT_SUMMARY`
- `SLIDING_WINDOW`
- `LLM_SUMMARIZE`
- `LLMLINGUA_COMPRESS`

#### 规则
- 先裁剪 session history
- 再压缩长 tool result
- 再考虑对早期历史做总结
- Saved Memories 默认不做有损压缩
- 压缩结果不回写 `SessionState` 与 `iteration_history`

### 14.7 `ContextEngineer`

#### 方法
- `prepare_context_for_llm(agent_state, context_materials) -> list[LLMMessage]`
- `set_skill_context(skill_prompt: str | None) -> None`
- `build_spawn_seed(session_messages, query, token_budget) -> list[LLMMessage]`
- `report_context_stats() -> ContextStats`

#### 只读约束
- 不允许修改 `SessionState`
- 不允许修改 `MemoryRecord`
- 不允许修改 `AgentState`
- 只读消费，绝不回写源状态

---

## 十五、Agent 编排层

### 15.1 `AgentConfig`
- `agent_id: str`
- `model_name: str`
- `system_prompt: str`
- `temperature: float`
- `max_output_tokens: int`
- `max_iterations: int`
- `allow_spawn_children: bool`

### 15.2 `CapabilityPolicy`
- `allowed_tool_categories: list[str] | None`
- `blocked_tool_categories: list[str] | None`
- `allow_network_tools: bool`
- `allow_system_tools: bool`
- `allow_spawn: bool`
- `max_spawn_depth: int`

### 15.3 `BaseAgent`

#### 字段
- `agent_id: str`
- `agent_config: AgentConfig`

#### Hook
- `on_before_run(task, agent_state) -> None`
- `on_iteration_started(iteration_index, agent_state) -> None`
- `on_tool_call_requested(tool_call_request) -> bool`
- `on_tool_call_completed(tool_result) -> None`
- `on_spawn_requested(spawn_spec) -> bool`
- `on_final_answer(answer, agent_state) -> None`
- `should_stop(iteration_result, agent_state) -> bool`

#### 策略方法
- `get_error_policy(error, agent_state) -> ErrorStrategy | None`
- `get_context_policy(agent_state) -> ContextPolicy`
- `get_memory_policy(agent_state) -> MemoryPolicy`
- `get_capability_policy() -> CapabilityPolicy`

---

### 15.4 `Skill`

#### 字段
- `skill_id: str`
- `name: str`
- `description: str`
- `trigger_keywords: list[str]`
- `system_prompt_addon: str`
- `model_override: str | None`
- `temperature_override: float | None`
- `recommended_capability_policy_id: str | None`

### 15.5 `SkillRouter`

#### 边界
- 只负责 skill registry 与 detection
- 不保存 active skill 运行态
- active skill 必须存于 run state 或 `AgentState`

#### 方法
- `register_skill(skill)`
- `detect_skill(user_input) -> Skill | None`
- `list_skills() -> list[Skill]`

### 15.6 Skill Override 生效边界

#### 配置分层
- `FrameworkConfig`：进程级默认配置
- `AgentConfig`：Agent 固有配置
- `EffectiveRunConfig`：当前 run 最终生效配置

#### 规则
- override 只在当前 run 生效
- run 结束后自动失效
- 子 Agent 默认不继承，除非 `SubAgentSpec.skill_id` 显式指定
- Skill override 只能覆盖白名单字段：`model_name`、`temperature`
- `max_iterations`、并发上限、子 Agent 配额等运行安全字段默认不可被 Skill 覆盖

### 15.7 `ErrorStrategy`
- `RETRY`
- `SKIP`
- `ABORT`

### 15.8 `RunStateController`
负责修改 run 级状态对象。

#### 职责
- 修改 `AgentState`
- 写入 `SessionState`
- 持有 active skill 引用
- 不负责策略推导

### 15.9 `RunPolicyResolver`
负责把配置与策略合成为当前 run 的有效结果。

#### 职责
- 生成 `EffectiveRunConfig`
- 决定当前 run 的 `ContextPolicy / MemoryPolicy / CapabilityPolicy`
- 不负责写状态

### 15.10 `AgentLoop`

#### 方法
- `execute_iteration(agent, deps, agent_state, llm_request) -> IterationResult`
- `_call_llm(deps, request) -> ModelResponse`
- `_check_stop_conditions(agent, model_response, agent_state) -> StopSignal | None`
- `_dispatch_tool_calls(agent, deps, tool_calls, agent_state) -> tuple[list[ToolResult], list[ToolExecutionMeta]]`
- `_handle_iteration_error(agent, error, agent_state) -> ErrorStrategy`

### 15.11 `RunCoordinator`

负责一次 run 的完整生命周期，并独占负责：

- skill 激活 / 去激活
- session 写入协调
- run 顺序编排

#### 方法
- `run(agent, deps, task) -> AgentRunResult`
- `_initialize_state(agent, task) -> AgentState`
- `_build_effective_config(agent, active_skill) -> EffectiveRunConfig`
- `_prepare_llm_request(agent, deps, agent_state) -> list[LLMMessage]`
- `_apply_skill_if_needed(agent, deps, task, agent_state) -> None`
- `_record_iteration(deps, session_state, iteration_result, agent_state) -> None`
- `_deactivate_skill_if_needed(deps) -> None`
- `_finalize_run(agent, agent_state, final_answer, stop_signal) -> AgentRunResult`
- `_handle_run_error(agent, error, agent_state) -> AgentRunResult`

#### 独占责任
- `RunCoordinator` 负责 skill 激活与去激活
- 正常结束与异常结束路径都必须执行 skill 清理
- `RunCoordinator` 是 `SessionState` 的唯一写入协调者

---

## 十六、子 Agent 运行时

### 16.1 原则
- 子 Agent 对主 Agent 表现为工具
- 默认隔离、默认最小权限、默认不可递归
- 结果必须摘要化返回
- 子 Agent 不直接写父 store

### 16.2 `SubAgentPolicyResolver`
负责子 Agent 策略解析。

#### 职责
- 解析子 Agent `CapabilityPolicy`
- 解析子 Agent `MemoryScope`
- 解析子 Agent `EffectiveRunConfig`

### 16.3 `SubAgentDependencyBuilder`
负责子 Agent 依赖构造。

#### 职责
- 创建子 scoped registry
- 创建子 session state
- 创建子 memory view 或独立 manager
- 创建子 runtime deps

### 16.4 `SubAgentFactory`
只负责装配，不直接承担业务规则。

#### 边界
- 不直接决定复杂 policy
- 不直接解释 memory 语义
- 不负责 artifact 生命周期治理

### 16.5 `MemoryScope` 调用路径

#### `ISOLATED`
- 子 Agent 使用独立 `MemoryManager + MemoryStore`
- 不读父 Saved Memories
- 不写父 Saved Memories

#### `INHERIT_READ`
- 子 Agent 使用只读 `ParentMemoryView`
- 只读父记忆，不可写回
- 读取的是 spawn 时刻的只读快照，不是实时共享视图

#### `SHARED_WRITE`
- 子 Agent 可以读取父记忆
- 默认读取的也是 spawn 时刻的只读快照
- 子 Agent 不直接写父 store
- 需要写回时，只能调用父 `MemoryManager.remember()`

#### 快照约束
- 子 Agent 运行期间不感知父记忆后续变化
- 默认不支持 live shared memory
- 这样可保证运行稳定性、可审计性与可重放性

### 16.6 `SubAgentScheduler`

#### 方法
- `submit(spec, factory, parent_agent) -> SubAgentHandle`
- `await_result(handle) -> SubAgentResult`
- `cancel(handle) -> None`
- `get_quota_status(parent_run_id) -> QuotaStatus`
- `_enforce_quota(parent_run_id) -> None`

### 16.7 `SubAgentRuntime`

#### 方法
- `spawn(spec, parent_agent) -> SubAgentResult`
- `get_active_children(parent_run_id) -> list[SubAgentHandle]`
- `cancel_all(parent_run_id) -> int`

---

## 十七、协议层

### 17.1 MCP 客户端 `MCPClientManager`

#### 开源借鉴
- `mcp` 官方 Python SDK

#### 方法
- `connect_server(server_config) -> str`
- `disconnect_server(server_id) -> None`
- `sync_tools_to_catalog(server_id, global_catalog) -> int`
- `call_mcp_tool(server_id, tool_name, arguments) -> Any`
- `load_config_file(path) -> list[MCPServerConfig]`
- `list_connected_servers() -> list[str]`

### 17.2 A2A 客户端 `A2AClientAdapter`

#### 开源借鉴
- `a2a-python` 官方 SDK

#### 方法
- `discover_agent(agent_url) -> AgentCard`
- `delegate_task_to_agent(agent_url, task_input, skill_id=None) -> SubAgentResult`
- `stream_task_to_agent(agent_url, task_input) -> AsyncIterator[LLMResponseChunk]`
- `register_as_a2a_server(agent, host, port) -> None`
- `list_known_agents() -> list[AgentCard]`

---

## 十八、配额语义

### 18.1 硬限制
超出后必须拒绝、停止或返回结构化错误：

- `max_sub_agents_per_run`
- `max_spawn_depth`
- 权限相关限制
- 安全相关确认拒绝

### 18.2 软限制
超出后允许降级处理：

- context token budget
- session history 保留量
- memory 注入条数
- 早期历史压缩

### 18.3 统一规则
- 硬限制不得 silently ignore
- 软限制必须使用确定性降级策略
- 降级结果必须可审计

---

## 十九、接入层与框架内核边界

### 19.1 框架内核负责
- Agent runtime
- tool execution
- context engineering
- memory management
- subagent orchestration
- delegation abstraction
- policy enforcement

### 19.2 接入层负责
- 用户鉴权
- session id 生成与映射
- API DTO 转换
- UI 展示
- websocket / streaming 输出
- 管理工具集暴露策略
- memory admin 工具是否开放给模型

---

## 二十、完整数据流

### 20.1 单 Agent 运行流

```text
用户输入
  │
  ▼
RunCoordinator.run(agent, deps, task)
  │
  ├─ initialize AgentState
  ├─ create SessionState
  ├─ detect skill
  ├─ build EffectiveRunConfig
  │
  ├─ [ITERATION LOOP]
  │     ├─ memory_manager.select_for_context()
  │     ├─ collect SessionState messages
  │     ├─ ContextEngineer.prepare_context_for_llm()
  │     ├─ ToolRegistry.export_schemas()
  │     ├─ AgentLoop.execute_iteration()
  │     ├─ RunCoordinator._record_iteration()
  │     └─ stop check
  │
  ├─ memory_manager.record_turn()
  ├─ memory_manager.end_session()
  ├─ deactivate skill
  └─ return AgentRunResult
```

### 20.2 子 Agent 派生流

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
  ├─ policy resolve
  ├─ dependency build
  ├─ SubAgentFactory.create()
  ├─ SubAgentScheduler.submit()
  ├─ await_result()
  └─ DelegationSummary -> ToolResult.output
```

---

## 二十一、目录结构

```text
agent_framework/
│
├── models/
│   ├── message.py
│   ├── tool.py
│   ├── agent.py
│   ├── session.py
│   ├── memory.py
│   ├── policy.py
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
│   ├── run_state_controller.py
│   ├── run_policy_resolver.py
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
│   ├── policy_resolver.py
│   ├── dependency_builder.py
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

## 二十二、推荐依赖

| 模块 | 依赖 | 外部服务依赖 | 用途 |
|------|------|--------------|------|
| LLM 调用 | `litellm` | 本地模型可离线 | 多模型适配 |
| Token 计数 | `tiktoken` | 否 | token 统计 |
| 参数校验 | `pydantic v2` | 否 | schema 与校验 |
| 配置管理 | `pydantic-settings` | 否 | 配置加载 |
| 持久化 | `sqlite3` | 否 | 默认记忆存储 |
| 上下文压缩 | `llmlingua` | 否 | 本地语义压缩 |
| 结构化日志 | `structlog` | 否 | JSON 日志 |
| 事件总线 | `blinker` | 否 | 解耦通知 |
| CLI | `click` | 否 | 命令行接入 |
| MCP | `mcp` 官方 SDK | 取决于 server | MCP 协议接入 |
| A2A | `a2a-python` 官方 SDK | 取决于对端 | A2A 协议接入 |

---

## 二十三、安全边界

### 23.1 工具权限
- 高风险工具必须 `require_confirm=True`
- 子 Agent 默认不可访问 `system / network`
- 工具权限由 `CapabilityPolicy` 控制，不由 Skill 控制

### 23.2 子 Agent 递归防护
- `SubAgentFactory` 强制 `allow_spawn_children=False`
- 子 Agent 调 `spawn_agent` 直接返回 `PERMISSION_DENIED`

### 23.3 记忆污染防护
- 默认只保存结构化 Saved Memories
- 不保存推理过程
- 不自动保存工具原始输出
- 子 Agent 不直接写父 store
- 用户可完全替换记忆策略

### 23.4 资源治理
- 子 Agent 总数配额
- 子 Agent 并发配额
- tool batch 并发上限
- deadline 超时取消

---

## 二十四、最小可实现路径

### Phase 1：最小单 Agent 闭环
- Pydantic 模型
- LiteLLMAdapter
- `@tool`
- ToolCatalog / ToolRegistry / ToolExecutor
- SessionState
- ContextBuilder
- AgentLoop + RunCoordinator

### Phase 2：简化记忆层
- `MemoryStoreProtocol`
- `SQLiteMemoryStore`
- `BaseMemoryManager`
- `DefaultMemoryManager`
- Saved Memories 注入上下文

### Phase 3：Skill 与能力策略
- SkillRouter
- CapabilityPolicy
- ScopedToolRegistry
- run-scoped effective config

### Phase 4：子 Agent
- DelegationExecutor
- SubAgentRuntime
- SubAgentFactory
- SubAgentScheduler

### Phase 5：协议接入
- MCPClientManager
- A2AClientAdapter

### Phase 6：规范收口
- 错误码注册表
- Message sanitization
- Policy only-reader boundary
- DTO serialization tests
- Session projection tests

---

## 二十五、实现约束总结

1. `RunCoordinator` 负责 run 生命周期，`AgentLoop` 只负责 iteration。  
2. `BaseAgent` 负责策略与业务钩子，运行依赖由 `AgentRuntimeDeps` 承载。  
3. 除 `RunCoordinator` 外，任何模块不得继续向下传递完整 `AgentRuntimeDeps`。  
4. 进程级、Agent 级、Run 级、SubAgent Run 级对象作用域必须严格分离。  
5. 记忆层默认只做 Saved Memories，不做全文检索。  
6. 默认记忆实现只依赖数据库，不引入 `rank_bm25`。  
7. `BaseMemoryManager` 与 `MemoryStoreProtocol` 是记忆层主干。  
8. 去重与合并逻辑只在 `MemoryManager` 中实现，不进入 store。  
9. 记忆层不做 prompt 格式化，格式化由上下文层完成。  
10. Session History 必须由 `SessionState` 唯一持有，且由 `RunCoordinator` 协调写入。  
11. `iteration_history` 是不可改写的结构化审计轨迹。  
12. 工具权限优先级固定为：`CapabilityPolicy` → `ScopedToolRegistry` → `on_tool_call_requested()`。  
13. `ScopedToolRegistry` 不是安全边界，执行时仍必须再次校验权限。  
14. `ContextPolicy` 与 `MemoryPolicy` 属于 run-scoped policy。  
15. `ContextPolicy` 只能由 `ContextEngineer` 解释，`MemoryPolicy` 只能由 `MemoryManager` 解释。  
16. Skill override 只作用于当前 run 的 `EffectiveRunConfig`，且仅能覆盖白名单字段。  
17. `EffectiveRunConfig` 在 run 开始后视为只读。  
18. 子 Agent 默认不继承父 Skill override。  
19. `ToolMeta.parameters_schema` 注册后冻结。  
20. `ToolResult.output` 必须是可 JSON 序列化 DTO。  
21. `spawn_agent` 返回给 LLM 的必须是摘要结果。  
22. `DelegationExecutor` 是所有委派动作的统一入口。  
23. 本地 subagent 与 A2A 的失败语义必须统一映射。  
24. `SHARED_WRITE` 只能通过父 `MemoryManager.remember()` 写回。  
25. 子 Agent 读取父记忆默认使用只读快照，而不是实时视图。  
26. 自动记忆提取只允许在 turn 结束时触发一次。  
27. `MemoryCandidate` 必须带来源类型与置信度。  
28. 默认记忆风格要接近 GPT Saved Memories，而不是知识库检索系统。  
29. 用户必须能够列出、删除、启停和钉住自己的 Saved Memories。  
30. 记忆治理接口默认不暴露给 LLM。  
31. `ContextEngineer` 只读，不允许回写源状态。  
32. `batch_execute()` 输出顺序必须与输入顺序一致。  
33. Message 进入模型前必须完成 sanitization。  
34. 日志不得成为业务状态通道。  
35. 硬限制必须拒绝或停止，软限制允许降级。  
36. `None` 只表示语义上不存在，不表示失败。  
37. Skill 的激活与去激活必须由 `RunCoordinator` 独占负责。  
38. 接入层与框架内核必须分离，用户鉴权与 UI 暴露策略不属于内核职责。  

---

## 二十六、结语

v2.5 的目标不是再扩展功能，而是把这份文档真正收口成一份 **可约束多人实现的工程规范**：

- 运行态对象作用域清楚
- 记忆、上下文、工具、委派边界清楚
- 可见性与安全边界分离
- 配额、错误码、None 语义、返回顺序全部有硬约束
- 默认实现简单，但高级能力仍可替换

以这份 v2.5 为基线，可以直接进入代码骨架生成与模块级测试设计阶段。


---

## 二十七、v3 正式并入规范（与前文冲突时以本章为准）

### 27.1 v3 目标

v3 的目标不是推翻 v2.5，而是在保持主干结构稳定的前提下，彻底收口以下问题：

- 运行时唯一状态写口
- 有效配置唯一生成入口
- 消息投影与状态写入解耦
- 并发结果串行提交
- 子 Agent 委派入口统一
- Session / Run / Turn 生命周期统一
- 工具事务组元数据前移
- 系统提示词前缀不可变
- 上下文层只消费只读快照
- hooks / plugins / marketplaces 的扩展能力纳入框架治理边界

### 27.2 v3 总体修订结论

v3 在工程上采用以下最终架构立场：

- `RunCoordinator` 是唯一 run 生命周期编排者
- `RunStateController` 是唯一运行时状态写口
- `RunPolicyResolver` 是唯一 `EffectiveRunConfig` 生成入口
- `MessageProjector` 负责结构化结果到消息的投影
- `CommitSequencer` 负责所有并发结果的稳定串行提交
- `DelegationExecutor` 是所有委派动作统一入口
- `SubAgentRuntime` 只负责本地子 Agent 生命周期
- `ContextSourceProvider` 只消费持久化事务元数据与只读快照
- `SystemPromptPrefix` 独立于 `SessionState`
- 所有架构守卫规则必须通过自动化测试长期防回退

### 27.3 最终依赖方向

所有主依赖方向都必须向下，不允许形成下游驱动上游：

- `RunCoordinator -> RunPolicyResolver`
- `RunCoordinator -> AgentLoop`
- `RunCoordinator -> RunStateController`
- `RunCoordinator -> BaseAgent`
- `RunStateController -> MessageProjector`
- `RunStateController -> CommitSequencer`
- `AgentLoop -> ModelAdapter`
- `AgentLoop -> ToolExecutor`
- `ToolExecutor -> DelegationExecutor`（若工具路由命中委派工具）
- `DelegationExecutor -> SubAgentRuntime | A2AClientAdapter`

不允许出现以下误导性关系：

- `AgentLoop -> RunCoordinator`
- `SkillRouter -> AgentLoop`
- `SessionState -> ContextEngineer` 的反向驱动
- `SubAgentRuntime` 自行解释父级 policy / quota

---

## 二十八、v3 运行时编排正式定义

### 28.1 `RunCoordinator`

`RunCoordinator` 负责一次 run 的完整生命周期，并独占负责：

- run 初始化
- run 级 policy bundle 获取
- 调用 `AgentLoop` 驱动单轮执行
- 调用 `RunStateController` 推进状态
- 控制 skill 激活 / 去激活时机
- 正常结束与异常结束的统一收尾
- 调用 `MemoryManager.begin_run_session / record_turn / end_run_session`

`RunCoordinator` 不得：

- 直接改写 `AgentState`
- 直接写 `SessionState`
- 直接构造 `EffectiveRunConfig`
- 直接解释 `ContextPolicy / MemoryPolicy / CapabilityPolicy`

### 28.2 `RunStateController`

`RunStateController` 是唯一运行时状态写口，负责：

- 修改 `AgentState`
- 写入 `SessionState`
- 持有并切换 `active_skill`
- 维护 iteration 计数、stop signal、错误状态
- 持有与落盘消息 / 审计 / 事务组关系

其他模块不得绕过它直接写：

- `AgentState`
- `SessionState`
- iteration history
- active skill

### 28.3 `RunPolicyResolver`

`RunPolicyResolver` 是唯一 `EffectiveRunConfig` 生成入口，负责统一合成：

- `AgentConfig`
- skill override
- run-scoped policy
- capability 上界
- memory / context 解释结果

其输出必须是冻结对象：

- `ResolvedRunPolicyBundle`
- `EffectiveRunConfig`

除 `RunPolicyResolver` 外，任何模块不得再次 merge config / policy。

### 28.4 `MessageProjector`

`MessageProjector` 只负责把结构化结果投影为会话消息，不做任何写入动作。

负责投影：

- `ModelResponse -> assistant message`
- `ToolResult -> tool message`
- `SubAgentResult -> delegation summary message`
- 最终答案 -> final assistant message

每条投影消息必须带：

- `message_id`
- `iteration_id`
- `transaction_group_id`（若属于事务组）
- `projection_version`

### 28.5 `CommitSequencer`

所有并发结果只允许并发计算，不允许并发提交。

`CommitSequencer` 负责：

- 收集并发结果
- 按稳定顺序排序
- 将结果提交给 `RunStateController`
- 保证 session/audit/artifact 的提交顺序与规则一致

允许并发的仅限：

- 工具执行计算
- 子 Agent 并发运行
- 后台异步通知 / 测试 / 审计

不允许并发提交的包括：

- `SessionState` 写入
- artifact 正式登记
- audit 真相源落盘
- 父 run 状态推进

---

## 二十九、v3 Session / Run / Turn 生命周期统一

### 29.1 统一术语

在 v3 中默认采用以下术语：

- `Conversation Turn`：用户一次输入到最终回答的完整闭环
- `Run`：一次 runtime 执行实例
- `RunSession`：当前 run 的会话状态容器

默认关系：

- 一个 `Conversation Turn` 对应一个 `Run`
- 一个 `Run` 对应一个 `RunSession`

### 29.2 术语约束

文档中的 `SessionState` 在 v3 中应理解为 `RunSessionState`。

若未来支持跨多个 turn 的长会话容器，必须单独定义：

- `ConversationSession`

当前版本不默认启用此概念。

### 29.3 Memory 生命周期

`MemoryManager` 必须使用：

- `begin_run_session(run_session_id)`
- `record_turn(run_session_id, turn_record)`
- `end_run_session(run_session_id, outcome)`

规则：

- `begin_run_session` 与 `end_run_session` 必须成对
- `end_run_session` 必须 finally 执行
- `record_turn` 是否提交取决于结构化提交条件与 `MemoryPolicy`
- memory 提交失败不得阻断 run finalize

---

## 三十、v3 事务组、审计与重试版本链

### 30.1 事务组真相源前移

`ToolTransactionGroup` 的身份必须在投影与落盘阶段显式生成，而不是在上下文层事后推断。

每条属于事务组的消息都必须带：

- `transaction_group_id`
- `transaction_role`

`ContextSourceProvider` 只能消费：

- `TransactionGroupIndex`
- `SessionSnapshot`
- `IterationAuditRecord`

不得从线性 `SessionState.messages` 猜测重建事务组。

### 30.2 iteration 与消息对齐

每次 iteration 必须生成唯一 `iteration_id`。

以下对象都必须可追溯到该 `iteration_id`：

- `SessionMessage`
- `IterationAuditRecord`
- `ToolTransactionGroup`
- `SubAgentResult`
- `DelegationSummary`

### 30.3 重试版本链

重试不得覆盖原记录。

必须区分：

- `iteration_id`：逻辑轮次
- `attempt_id`：具体尝试
- `transaction_group_id`：逻辑事务组
- `group_attempt_id`：事务组具体尝试

审计层必须保留完整 attempt 链；用户或模型可只看到最终成功结果。

---

## 三十一、v3 工具执行与委派执行

### 31.1 工具执行的可观察副作用

并发工具执行时，可观察副作用必须经过统一提交器后才可见。

可观察副作用包括：

- Session 写入
- artifact 登记
- 审计落盘
- 对外部系统写操作结果回填
- 进入记忆治理链的结构化结果

### 31.2 自动重试幂等边界

自动重试不是默认安全行为。

自动重试必须同时满足：

- `retryable = true`
- 且以下至少一项成立：
  - `idempotent = true`
  - 存在稳定 `idempotency_key`

外部写操作工具默认不允许自动重试，除非显式声明幂等键。

### 31.3 `DelegationExecutor`

`DelegationExecutor` 是所有委派动作的统一入口，负责：

- 统一委派权限检查
- 统一 spawn 配额检查
- 统一选择本地子 Agent 或远端 A2A
- 统一错误码与返回结构
- 统一本地与远端委派状态机

### 31.4 委派状态机

本地子 Agent 与远端 A2A 都必须使用统一 `SubAgentStatus`：

- `COMPLETED`
- `FAILED`
- `CANCELLED`
- `REJECTED`
- `DEGRADED`

错误码映射示例：

- `TIMEOUT -> FAILED`
- `QUOTA_EXCEEDED -> REJECTED`
- `PERMISSION_DENIED -> REJECTED`
- `DELEGATION_FAILED -> FAILED`
- `REMOTE_UNAVAILABLE -> FAILED`

### 31.5 子 Agent 结果分层

必须区分三层结果：

- `SubAgentRawResult`：仅供内部使用，含原始轨迹
- `SubAgentResult`：供父运行时消费
- `DelegationSummary`：仅供父 LLM / 父 session 可见

禁止将以下内容直接注入父上下文：

- 子 Agent 全量 session
- 原始 tool trace
- 原始错误栈
- 远端原始协议体

---

## 三十二、v3 多 Agent 协同正式方案

### 32.1 设计立场

多 Agent 协同采用“父决策、子受控执行、统一回传”的树状委派模型，不采用自由互调的网状结构。

### 32.2 核心组件

多 Agent 协同核心组件包括：

- `DelegationExecutor`
- `SubAgentRuntime`
- `SubAgentScheduler`
- `SubAgentPolicyResolver`
- `SubAgentDependencyBuilder`
- `SubAgentFactory`
- `A2AClientAdapter`

### 32.3 `SubAgentScheduler` 与 `SubAgentRuntime` 边界

`SubAgentScheduler` 只负责：

- 排队
- 并发控制
- 配额判断
- 分配 `subagent_task_id`

`SubAgentRuntime` 只负责：

- 实际执行
- active children 真相源
- 取消传播
- 资源回收
- 返回 `SubAgentResult`

两者不得各自维护第二份 active child 真相源。

### 32.4 子 Agent 配置 override

`SubAgentSpec.agent_config_override` 在 v3 中必须使用白名单强类型对象，不得使用原始 `dict` 通用注入口。

允许覆盖字段建议限定为：

- `model_name`
- `temperature`
- `system_prompt_addon`

禁止覆盖：

- 父 run 配额
- 权限上界
- session / state / store 引用
- 记忆治理开关
- 审计开关

### 32.5 `tool_category_whitelist`

`tool_category_whitelist` 只能收窄子 Agent 可见工具类别，不得扩大权限。

子 Agent 实际可用工具类别必须取交集：

- 父 `CapabilityPolicy`
- 子 `CapabilityPolicy`
- `tool_category_whitelist`
- 当前 registry 可见集合

### 32.6 `SubAgentDependencyBuilder`

`SubAgentDependencyBuilder` 只能消费已解析结果，不得自行解释：

- `MemoryScope`
- `CapabilityPolicy`
- `EffectiveRunConfig`
- `SubAgentConfigOverride`
- quota 决策结果

解释权归属：

- `SubAgentPolicyResolver`：解释子运行策略与配置
- `SubAgentScheduler`：解释调度与配额
- `SubAgentDependencyBuilder`：只做依赖装配

---

## 三十三、v3 不可变系统前缀（Immutable System Prefix）

### 33.1 设计目标

为保证：

- 用户首次访问请求时加载完整系统提示词
- 后续请求系统提示词始终位于首位
- 记忆压缩和 session 裁剪永不影响 system prompt
- 为底层 prefix caching / KV cache 复用提供稳定前缀

v3 引入不可变系统前缀机制。

### 33.2 基本原则

系统提示词不属于 `SessionState`，而属于独立的只读前缀层：

- 不进入事务组
- 不进入历史压缩
- 不进入 Saved Memory
- 不参与 session 裁剪
- 永远作为 prompt 第 0 段输出

### 33.3 新增对象

```python
@dataclass(frozen=True)
class SystemPromptPrefix:
    prefix_id: str
    agent_id: str
    version: str
    messages: tuple[LLMMessage, ...]
    canonical_text_hash: str
    created_at: datetime
```

### 33.4 新增前缀仓库

```python
class SystemPromptPrefixStore(Protocol):
    def get(prefix_key: str) -> SystemPromptPrefix | None: ...
    def save(prefix_key: str, prefix: SystemPromptPrefix) -> None: ...
```

建议 `prefix_key` 只包含真正影响前缀 token 序列的因素：

- `agent_id`
- `system_prompt_version`
- `model_family`
- `tool_schema_version`（如 system 固定依赖）
- `safety_policy_version`（如固定依赖）

### 33.5 上下文槽位升级

v3 将上下文槽位改为：

- Prefix Slot 0: Immutable System Prefix
- Slot 1: Skill Addon
- Slot 2: Saved Memories
- Slot 3: Session History
- Slot 4: Current Input

规则：

- `Immutable System Prefix` 不参与 budget 裁剪
- `Immutable System Prefix` 不参与压缩
- `Immutable System Prefix` 永远首位固定
- skill addon 仍属于可变层，不与 immutable prefix 混合

### 33.6 前缀稳定性与缓存复用说明

框架层只能保证：

- 系统前缀字节级稳定
- 每次请求的首段完全一致
- 前缀 hash 与 version 可追踪

底层模型服务是否实际复用 KV cache / prefix cache，取决于推理后端能力。框架不承诺底层服务一定复用，但必须为复用创造稳定前提。

---

## 三十四、v3 上下文层正式修订

### 34.1 上下文层只读边界

`ContextSourceProvider` 与 `ContextEngineer` 不得直接消费可变 `SessionState`，只能消费：

- `SessionSnapshot`
- `SessionReadView`
- `TransactionGroupIndex`

### 34.2 上下文压缩边界

上下文压缩只允许作用于：

- Saved Memories
- Session History
- Tool result summaries
- Delegation summaries

不得压缩或改写：

- immutable system prefix
- skill addon 原始规则
- 审计真相源
- session 持久化消息正文

### 34.3 快照一致性

同一次上下文构建必须绑定单一 `SessionSnapshot.version`，不得在构建过程中读取实时变化中的 session。

---

## 三十五、v3 Artifact 生命周期

### 35.1 统一 artifact 管理者

所有 artifact 的登记、引用、保留期与清理由统一 `ArtifactManager` 负责。

工具与子 Agent 只允许“产出 artifact”，不允许自行定义长期生命周期策略。

### 35.2 建议对象

```python
@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    uri: str
    media_type: str
    producer_kind: str
    producer_run_id: str
    retention_policy: str
    expires_at: datetime | None
```

### 35.3 清理规则

- run 结束时，`RunCoordinator` 必须调用 `ArtifactManager.finalize_run_artifacts(run_id)`
- 子 Agent artifact 不得脱离父 run 的可追踪范围
- 清理失败必须进入审计日志

---

## 三十六、v3 错误、终止与恢复立场

### 36.1 终止语义分层

运行结束原因必须严格区分：

#### `StopSignal`
表示正常停止：
- `FINAL_ANSWER_READY`
- `MAX_ITERATIONS_REACHED`
- `AGENT_REQUESTED_STOP`
- `USER_CANCELLED`

#### `AbortReason`
表示失败终止：
- `MODEL_CALL_FAILED`
- `TOOL_EXECUTION_FAILED`
- `POLICY_VIOLATION`
- `STATE_CORRUPTION`
- `UNRECOVERABLE_DELEGATION_ERROR`

#### `DegradeReason`
表示降级继续：
- `SOFT_TOKEN_BUDGET_EXCEEDED`
- `CONTEXT_COMPRESSED`
- `TOOL_PARALLELISM_REDUCED`
- `SOFT_SUBAGENT_QUOTA_REACHED`

### 36.2 最终错误出口

`AgentRunResult.error` 在 v3 中不得再是裸字符串，必须使用结构化 `RunErrorInfo`：

```python
@dataclass(frozen=True)
class RunErrorInfo:
    error_code: str
    message: str
    source: str
    retryable: bool
    user_visible: bool
    audit_ref: str | None
```

### 36.3 checkpoint / resume 立场

v3 默认 **不支持通用 run-level resume**，除非显式引入 checkpoint 机制。

未引入 `RunCheckpoint` 之前，不得使用以下对象作为恢复真相源：

- `SessionState`
- `iteration_history`
- `EventBus` 事件
- `StructLogger` 日志
- websocket / streaming 缓冲输出

---

## 三十七、v3 事件、日志与 hooks / plugins 扩展治理

### 37.1 事件总线立场

`EventBus` 只用于观测，不得承载业务真值。

事件投递语义：

- 尽力而为
- 允许重复
- 允许丢失
- 订阅方必须按 `event_id` 幂等处理
- 事件顺序不得充当核心状态机推进依据

### 37.2 日志立场

`StructLogger` 只记录事实，不驱动业务逻辑，不可作为运行态恢复真相源。

### 37.3 hooks 治理立场

v3 推荐将外部自动化与策略扩展做成插件化 hooks，而不是将大量治理逻辑写入内核。

适合通过 hooks 承担的职责：

- PreToolUse 强约束拦截
- PostToolUse 格式化 / 测试 / 通知
- Notification 人机协作提醒
- ConfigChange / InstructionsLoaded 审计
- TaskCompleted / Stop 完成态门禁
- SubagentStart / SubagentStop 子代理审计

### 37.4 hooks 开发原则

- 必须每次发生、确定性规则：优先 command hooks
- 需要 yes/no 智能判定：使用 prompt hooks
- 需要更复杂工具辅助验证：使用 agent hooks
- 长耗时动作：使用异步 command hooks
- 不得把 hooks 变成框架核心状态真相源

### 37.5 插件与 marketplace

团队分发 hooks / agents / commands / MCP servers 时，推荐：

- 先本地验证
- 再封装为 Claude Code 插件
- 再通过 marketplace 统一分发

---

## 三十八、v3 架构守卫、故障注入与验收要求

### 38.1 架构守卫测试

必须至少覆盖以下守卫：

- `SessionState` 只有 `RunStateController` 可写
- `AgentLoop` 零写入
- `RunCoordinator` 不解释原始 policy 字段
- `RunCoordinator` 不直接构造 `EffectiveRunConfig`
- `SubAgentScheduler` 不维护 active runtime 真相源
- `SubAgentDependencyBuilder` 不解释 policy / config / quota
- `ContextSourceProvider` 不重建事务组
- `ToolCommitSequencer` 顺序稳定
- DTO / frozen 对象不可变
- EventBus observation-only

### 38.2 故障注入测试

必须验证以下路径全部能正确收口：

- 模型 500 / adapter 异常
- 工具部分失败
- 子 Agent timeout / cancellation / rejection
- memory commit 失败
- `end_run_session` 失败
- streaming 中断
- event 重复投递
- artifact finalize 失败

### 38.3 数据流不变量

必须验证以下不变量：

- iteration history append-only
- 每条消息带 `iteration_id`
- SessionSnapshot 不可变
- attempt 链可追踪
- whitelist 不扩权
- error code -> status 映射完备
- begin / end_run_session 成对
- immutable system prefix 稳定不变

### 38.4 真实违规修复要求

若守卫测试发现真实旁路，例如：

- `RunCoordinator` 直接调用 `session_state.append_message`
- 其他模块直接写 `SessionState`
- builder 偷偷 merge config
- context 层生成新的 transaction_group_id

必须视为架构违规并修复，而非在文档中豁免。

---

## 三十九、v3 推荐目录增补

在原目录结构基础上，v3 建议增补以下模块：

```text
agent_framework/
├── runtime/
│   ├── coordinator.py
│   ├── state_controller.py
│   ├── policy_resolver.py
│   ├── message_projector.py
│   ├── commit_sequencer.py
│   ├── session_snapshot.py
│   └── identities.py
├── delegation/
│   ├── executor.py
│   ├── status.py
│   ├── scheduler.py
│   ├── runtime.py
│   ├── policy_resolver.py
│   ├── dependency_builder.py
│   └── a2a_adapter.py
├── context/
│   ├── source_provider.py
│   ├── transaction_index.py
│   ├── builder.py
│   ├── engineer.py
│   └── system_prefix.py
├── memory/
│   ├── manager.py
│   ├── store_protocol.py
│   ├── sqlite_store.py
│   └── snapshot_view.py
├── artifacts/
│   ├── manager.py
│   └── refs.py
├── hooks/
│   ├── plugin_hooks.md
│   └── examples/
└── tests/
    ├── test_architecture_guard.py
    ├── test_fault_injection.py
    ├── test_dataflow_invariants.py
    └── test_delegation_status_machine.py
```

---

## 四十、v3 结论

v3 的核心不是增加更多能力，而是让框架主干满足以下最终标准：

- 单 Agent 主循环稳定
- 多 Agent 委派可控
- 状态写口唯一
- 配置解释唯一
- 上下文构建只读、可压缩、可追踪
- 系统前缀不可变且可缓存复用
- hooks / plugins 扩展受治理
- 自动化测试可以长期防回退

当实现满足以上约束时，框架即可进入：

- 联调
- 压测
- 试运行
- 团队扩展与插件化治理


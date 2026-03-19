下面给你一版可直接并入现有 v3 文档的 **“主 Agent / 子 Agent 长时交互机制”** 实现方案，重点解决：

* 主子 Agent 不是一次性 `spawn -> wait -> return`
* 支持长时间运行、分阶段回传、暂停/恢复、HITL
* 不破坏你现有的 v3 原则：

  * `RunCoordinator` 统一编排
  * `RunStateController` 唯一写口
  * `CommitSequencer` 串行提交
  * `DelegationExecutor` 统一委派入口
  * 子 Agent 不成为父状态真相源
  * 父子默认不共享可写状态

---

# AI Agent Framework — 主 Agent / 子 Agent 长时交互机制

> **定位**：为 v3 增补父子 Agent 的长时协作、分阶段回传、暂停/恢复、人工介入（HITL）机制
> **目标**：让子 Agent 可以从“一次性任务执行器”升级为“受控的长时工作单元”，同时不破坏现有状态边界与统一提交链

---

## 一、问题定义

现有 v3 文档中的子 Agent 机制，本质上偏向：

* 父发起委派
* 子完成执行
* 子返回 `SubAgentResult`
* 父收到 `DelegationSummary`

这适合一次性短任务，但不够支撑以下场景：

1. 子 Agent 运行时间很长
2. 子 Agent 需要多次中间回报
3. 父 Agent 需要根据中间结果继续决策
4. 子 Agent 需要请求用户确认或额外输入
5. 子 Agent 执行中可能暂停，等待外部事件再恢复
6. 父 Agent 需要同时管理多个长时子任务
7. A2A 远端代理与本地 subagent 需要统一状态语义

因此需要正式引入：

* **长时交互状态机**
* **父子消息通道**
* **挂起 / 恢复机制**
* **阶段性结果回传**
* **HITL 协调机制**
* **统一的本地 / A2A 长时协议抽象**

---

## 二、设计目标

### 2.1 必须实现

* 父 Agent 可启动长时子 Agent
* 子 Agent 可分阶段上报进展
* 父 Agent 可轮询或接收事件驱动更新
* 子 Agent 可请求更多输入、确认、授权
* 子 Agent 可挂起并恢复
* 父 Agent 可取消子 Agent
* 本地 subagent 与 A2A 远端状态统一

### 2.2 必须保持

* 父 Agent 仍是决策者
* 子 Agent 仍是受控执行者
* 所有正式状态变更通过统一提交链
* 子 Agent 不能直接写父 Session / AgentState
* 子 Agent 默认只读父快照，不读实时共享状态

### 2.3 明确不做

* 不做自由网状多 Agent 互调
* 不做父子共享实时可写记忆
* 不做子 Agent 直接操作父上下文
* 不做以 EventBus 充当父子真相通道

---

## 三、核心设计立场

### 3.1 父决策，子执行，协调器统一提交

长时交互中，子 Agent 只负责：

* 执行
* 中间报告
* 请求输入/确认
* 等待恢复

父 Agent 只负责：

* 根据中间结果做决策
* 提供补充输入
* 决定是否继续/取消/降级

真正的正式状态推进由：

* `DelegationExecutor`
* `RunCoordinator`
* `RunStateController`
* `CommitSequencer`

统一完成。

### 3.2 长时交互不是共享聊天

父子之间不能直接共享一个会话缓冲区。
必须通过 **结构化交互通道** 交换消息。

### 3.3 子 Agent 的中间结果不是最终上下文真相源

子 Agent 可产生很多中间消息，但进入父 LLM 可见上下文的，只能是：

* `DelegationEventSummary`
* `SubAgentProgressUpdate`
* `SubAgentQuestion`
* `SubAgentCheckpointNotice`
* `SubAgentFinalResult`

而不是子 Agent 全量 session。

---

## 四、核心能力拆分

建议把长时交互拆成 5 个子能力：

1. **长时子任务状态机**
2. **父子交互消息协议**
3. **挂起 / 恢复（suspend / resume）**
4. **HITL 请求机制**
5. **本地 subagent 与 A2A 的统一适配**

---

# 五、长时子任务状态机

---

## 5.1 新增状态枚举

你现有 `SubAgentHandle.status` 太粗，建议升级为：

```python id="gwzsvh"
class SubAgentStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    WAITING_PARENT = "WAITING_PARENT"
    WAITING_USER = "WAITING_USER"
    SUSPENDED = "SUSPENDED"
    RESUMING = "RESUMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    DEGRADED = "DEGRADED"
    TIMEOUT = "TIMEOUT"
```

### 状态含义

* `PENDING`：已提交，未开始执行
* `RUNNING`：正常执行中
* `WAITING_PARENT`：子 Agent 等待父 Agent 补充信息
* `WAITING_USER`：子 Agent 通过父链路请求用户输入或确认
* `SUSPENDED`：挂起，等待恢复
* `RESUMING`：已收到恢复指令，正在重新进入执行
* `COMPLETED`：正常完成
* `FAILED`：失败终止
* `CANCELLED`：被取消
* `REJECTED`：因权限/配额/策略被拒绝
* `DEGRADED`：被软降级
* `TIMEOUT`：超时结束

---

## 5.2 状态迁移约束

允许的主迁移：

```text id="lsahxt"
PENDING -> RUNNING
RUNNING -> WAITING_PARENT
RUNNING -> WAITING_USER
RUNNING -> SUSPENDED
RUNNING -> COMPLETED
RUNNING -> FAILED
RUNNING -> TIMEOUT
RUNNING -> DEGRADED
WAITING_PARENT -> RESUMING
WAITING_USER -> RESUMING
SUSPENDED -> RESUMING
RESUMING -> RUNNING
ANY ACTIVE -> CANCELLED
```

不允许：

* `COMPLETED -> RUNNING`
* `FAILED -> RESUMING`
* `CANCELLED -> RESUMING`

---

## 六、父子交互消息协议

---

## 6.1 新增事件模型

父子交互必须通过结构化事件，而不是随意文本拼接。

```python id="10wl7s"
class DelegationEventType(str, Enum):
    STARTED = "STARTED"
    PROGRESS = "PROGRESS"
    QUESTION = "QUESTION"
    CONFIRMATION_REQUEST = "CONFIRMATION_REQUEST"
    CHECKPOINT = "CHECKPOINT"
    ARTIFACT_READY = "ARTIFACT_READY"
    SUSPENDED = "SUSPENDED"
    RESUMED = "RESUMED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
```

```python id="w9x9xd"
class DelegationEvent(BaseModel):
    event_id: str
    spawn_id: str
    parent_run_id: str
    event_type: DelegationEventType
    created_at: datetime
    sequence_no: int
    payload: dict
    requires_ack: bool = False
```

### 设计要求

* `event_id` 全局唯一
* `sequence_no` 对同一 `spawn_id` 单调递增
* 事件必须 append-only
* 父侧不得从文本重建事件

---

## 6.2 常见 payload 结构

### Progress

```python id="d2qjw2"
{
  "summary": "Indexed 12 files, now analyzing auth flow",
  "progress_percent": 40,
  "current_step": "Analyze JWT verification"
}
```

### Question

```python id="k8d7rj"
{
  "question_id": "q_001",
  "question": "Should I modify production config files or only test environment files?",
  "options": ["test_only", "all_envs"],
  "suggested_default": "test_only"
}
```

### ConfirmationRequest

```python id="snyf2d"
{
  "request_id": "c_001",
  "reason": "This step requires running a write-capable shell command",
  "action_label": "Run migration script"
}
```

### Checkpoint

```python id="mam5o0"
{
  "checkpoint_id": "cp_001",
  "summary": "Search phase completed; ready to enter edit phase",
  "resume_token": "opaque_token_here"
}
```

---

## 七、长时交互句柄与通道

---

## 7.1 升级 `SubAgentHandle`

```python id="2msp72"
class SubAgentHandle(BaseModel):
    sub_agent_id: str
    spawn_id: str
    parent_run_id: str
    status: SubAgentStatus
    created_at: datetime
    updated_at: datetime
    last_event_seq: int = 0
    waiting_reason: str | None = None
    resume_token: str | None = None
```

---

## 7.2 新增交互通道协议

```python id="jlwm8d"
class SubAgentInteractionChannelProtocol(Protocol):
    def append_event(self, event: DelegationEvent) -> None: ...
    def list_events(
        self,
        spawn_id: str,
        after_sequence_no: int | None = None,
    ) -> list[DelegationEvent]: ...
    def ack_event(self, spawn_id: str, event_id: str) -> None: ...
```

### 作用

这是父子长时交互的真相通道。
不要用 `EventBus` 代替它。

---

## 八、挂起 / 恢复机制

---

## 8.1 设计目标

当子 Agent：

* 需要更多输入
* 等待用户确认
* 等待外部异步结果
* 到达阶段性 checkpoint

应支持安全挂起，而不是只能超时或失败。

---

## 8.2 新增挂起结果

```python id="j4pjon"
class SubAgentSuspendReason(str, Enum):
    WAIT_PARENT_INPUT = "WAIT_PARENT_INPUT"
    WAIT_USER_CONFIRMATION = "WAIT_USER_CONFIRMATION"
    WAIT_EXTERNAL_EVENT = "WAIT_EXTERNAL_EVENT"
    CHECKPOINT_PAUSE = "CHECKPOINT_PAUSE"
```

```python id="r4hamf"
class SubAgentSuspendInfo(BaseModel):
    reason: SubAgentSuspendReason
    message: str
    resume_token: str
    payload: dict | None = None
```

---

## 8.3 子 Agent 运行结果分层调整

现有只有 `SubAgentResult` 不够，建议拆分：

```python id="0bx9lb"
class SubAgentRawResult(BaseModel):
    spawn_id: str
    final_status: SubAgentStatus
    raw_events: list[DelegationEvent]
    internal_trace_ref: str | None = None
```

```python id="0e2v4w"
class SubAgentResult(BaseModel):
    spawn_id: str
    success: bool
    final_status: SubAgentStatus
    final_answer: str | None = None
    suspend_info: SubAgentSuspendInfo | None = None
    error: str | None = None
    artifacts: list[Artifact]
    usage: TokenUsage
    iterations_used: int
    duration_ms: int
    trace_ref: str | None = None
    error_code: str | None = None
```

### 关键点

`SubAgentResult` 不再只代表“结束结果”，也可能代表：

* 暂停
* 等待父输入
* 等待用户输入

---

## 8.4 恢复接口

### `SubAgentRuntimeProtocol` 扩展

```python id="y1iidu"
class SubAgentRuntimeProtocol(Protocol):
    def spawn(self, spec: SubAgentSpec, parent_agent: BaseAgent) -> SubAgentResult: ...
    def resume(
        self,
        spawn_id: str,
        resume_payload: dict,
        parent_agent: BaseAgent,
    ) -> SubAgentResult: ...
    def get_active_children(parent_run_id: str) -> list[SubAgentHandle]: ...
    def cancel_all(parent_run_id: str) -> int: ...
    def cancel(spawn_id: str) -> None: ...
```

### `DelegationExecutorProtocol` 扩展

```python id="sygvwb"
class DelegationExecutorProtocol(Protocol):
    def delegate_to_subagent(self, spec: SubAgentSpec, parent_agent: BaseAgent) -> SubAgentResult: ...
    def resume_subagent(
        self,
        spawn_id: str,
        resume_payload: dict,
        parent_agent: BaseAgent,
    ) -> SubAgentResult: ...
    def delegate_to_a2a(self, agent_url: str, task_input: str, skill_id: str | None = None) -> SubAgentResult: ...
    def resume_a2a(
        self,
        remote_task_id: str,
        resume_payload: dict,
    ) -> SubAgentResult: ...
```

---

## 九、HITL 协调机制

---

## 9.1 设计目标

子 Agent 不直接问用户。
子 Agent 只能通过父协调链提出问题。

### 原则

* 子问父
* 父再问用户
* 用户答父
* 父再恢复子

---

## 9.2 新增 HITL 请求模型

```python id="6xdkp8"
class HITLRequest(BaseModel):
    request_id: str
    spawn_id: str
    parent_run_id: str
    request_type: Literal["question", "confirmation", "clarification"]
    title: str
    message: str
    options: list[str] = []
    suggested_default: str | None = None
    created_at: datetime
```

```python id="d30zpo"
class HITLResponse(BaseModel):
    request_id: str
    response_type: Literal["answer", "confirm", "deny", "cancel"]
    answer: str | None = None
    selected_option: str | None = None
```

---

## 9.3 协调流程

```text id="7x0buv"
子 Agent 运行
  ↓
产生 QUESTION / CONFIRMATION_REQUEST 事件
  ↓
DelegationExecutor 转换为 HITLRequest
  ↓
RunCoordinator 通过接入层暴露给用户
  ↓
用户回答
  ↓
接入层把 HITLResponse 回传给父 run
  ↓
父 run 调用 resume_subagent()
  ↓
子 Agent 进入 RESUMING -> RUNNING
```

---

## 十、父 Agent 的长时交互模式

---

## 10.1 两种模式

### 模式 A：阻塞等待 + 中间事件

父 Agent 发起子 Agent 后，可在本轮等待一段时间，收集若干中间事件，再决定是否继续等待或结束本轮。

适合：

* 短时但多阶段任务

### 模式 B：非阻塞派发 + 后续恢复

父 Agent 发起子 Agent 后立即获得 `spawn_id`，子 Agent 后台继续运行；父 run 后续轮次通过通知事件感知进展。

适合：

* 长时任务
* 外部依赖任务
* HITL 任务

---

## 10.2 建议新增委派模式

```python id="1aujlwm"
class DelegationMode(str, Enum):
    BLOCKING = "BLOCKING"
    NON_BLOCKING = "NON_BLOCKING"
    INTERACTIVE = "INTERACTIVE"
```

在 `SubAgentSpec` 中增加：

```python id="ee5hqd"
delegation_mode: DelegationMode = DelegationMode.BLOCKING
```

### 解释

* `BLOCKING`：等待到完成/失败/挂起
* `NON_BLOCKING`：立即返回 handle/初始结果
* `INTERACTIVE`：允许中间多轮事件交换

---

## 十一、父上下文如何消费子事件

---

## 11.1 不允许直接注入子全量轨迹

禁止将以下内容直接注入父上下文：

* 子 Agent 全量 session
* 子 Agent 原始工具调用流
* 子 Agent 原始错误栈
* A2A 原始协议包

---

## 11.2 新增父可见摘要模型

```python id="d02gwr"
class DelegationEventSummary(BaseModel):
    spawn_id: str
    status: SubAgentStatus
    summary: str
    question: str | None = None
    checkpoint_notice: str | None = None
    error_code: str | None = None
    artifacts_digest: list[str] = []
```

### 投影规则

只有摘要进入父 Session 投影。

---

## 11.3 父会话投影顺序

建议固定为：

```text id="4ldwli"
assistant(tool call spawn/resume)
tool(delegation accepted/result)
assistant(optional narration)
```

若有中间事件异步注入，应通过：

* `background-results` 样式的受控 control message
* 或独立 `delegation-events` context material

但不应伪装成普通用户聊天。

---

## 十二、SubAgentScheduler / Runtime / Executor 边界修订

---

## 12.1 `SubAgentScheduler`

只负责：

* 排队
* 并发限制
* quota 检查
* 分配任务 ID
* 驱动 suspend/resume/cancel 调度

不负责：

* 保存第二份 active child 真相源
* 自行解释父策略

---

## 12.2 `SubAgentRuntime`

只负责：

* 实际执行
* 维护子任务生命周期
* 输出结构化事件
* 响应 suspend/resume/cancel

---

## 12.3 `DelegationExecutor`

是唯一外部入口，负责：

* 权限检查
* 配额检查
* 本地 vs A2A 路由
* 统一状态与错误码
* 统一长时交互接口

---

## 十三、本地 subagent 与 A2A 协议统一

---

## 13.1 一致性要求

即使 A2A SDK 与本地实现细节不同，在框架内必须统一映射为：

* `SubAgentHandle`
* `DelegationEvent`
* `SubAgentStatus`
* `SubAgentResult`
* `HITLRequest`
* `HITLResponse`

### 结论

框架层不要求协议字面一致，但要求**语义一致**。

---

## 13.2 A2A 适配层责任

`A2AClientAdapter` 必须负责把远端协议映射成本地统一语义：

* 远端 progress -> `DelegationEvent(PROGRESS)`
* 远端 requires_input -> `WAITING_PARENT` / `WAITING_USER`
* 远端 paused -> `SUSPENDED`
* 远端 final result -> `COMPLETED`
* 远端 transport error -> `FAILED`

---

## 十四、Checkpoint 机制

---

## 14.1 为什么要 checkpoint

长时子 Agent 若支持 suspend/resume，仅靠 in-memory 状态不够稳定。
建议引入轻量 checkpoint。

```python id="vfjwtz"
class SubAgentCheckpoint(BaseModel):
    checkpoint_id: str
    spawn_id: str
    created_at: datetime
    resume_token: str
    state_ref: str | None = None
    summary: str
```

---

## 14.2 最小要求

本期不要求通用 run-level resume，但对子 Agent 交互式恢复，建议至少支持：

* `resume_token`
* 子 Agent 当前阶段摘要
* 可恢复所需最小上下文引用

---

## 十五、配置需求

建议新增：

```python id="6w3o4c"
class LongInteractionConfig(BaseModel):
    enable_interactive_subagents: bool = True
    enable_suspend_resume: bool = True
    max_pending_hitl_requests_per_run: int = 5
    max_delegation_events_per_subagent: int = 200
    max_interactive_rounds_per_subagent: int = 20
    delegation_event_summary_limit: int = 10
```

---

## 十六、与 Todo / Background 机制的衔接

你前面已经有 Todo / background 设计，这里建议打通：

### 16.1 子 Agent 事件 -> 父 Todo

父 Agent 可根据以下事件更新 Todo：

* `PROGRESS`
* `CHECKPOINT`
* `COMPLETED`
* `FAILED`

但必须由父 Agent 或父控制面决定，不是子 Agent 直接写父 Todo。

### 16.2 子 Agent 事件 -> background notifications

非阻塞模式下，子 Agent 事件可以像 background task 一样，被父 run 的下一轮注入。

所以可以统一抽象：

```python id="5vq8xj"
class RuntimeNotification(BaseModel):
    notification_id: str
    notification_type: Literal["background_task", "delegation_event"]
    run_id: str
    payload: dict
```

---

## 十七、测试要求

---

## 17.1 架构守卫测试

必须覆盖：

* 子 Agent 不能直接写父 `SessionState`
* 子 Agent 不能直接写父 `TodoState`
* `DelegationExecutor` 是唯一 resume/cancel 入口
* `SubAgentRuntime` 不能自行解释父级 policy/quota
* 父上下文不注入子全量 session
* 事件序号严格递增

---

## 17.2 故障注入测试

必须覆盖：

* 子 Agent 运行中断
* suspend 后 resume token 无效
* 用户拒绝 HITL 请求
* A2A 远端断连
* progress 事件重复投递
* COMPLETED 后再次收到 progress
* cancel 与 resume 并发冲突

---

## 17.3 数据流不变量

必须验证：

* `spawn_id` 全程可追踪
* 事件 append-only
* `sequence_no` 单调递增
* `WAITING_USER` 只能由 question/confirmation 类事件触发
* `COMPLETED / FAILED / CANCELLED` 后不可恢复
* 父可见内容永远是摘要投影

---

## 十八、最小实现路径

### Phase I1：长时状态机

* `SubAgentStatus`
* `DelegationEvent`
* `SubAgentInteractionChannel`
* 非阻塞子任务句柄

### Phase I2：挂起 / 恢复

* `SUSPENDED / RESUMING`
* `SubAgentSuspendInfo`
* `resume_subagent()`

### Phase I3：HITL

* `HITLRequest / HITLResponse`
* `WAITING_USER`
* 父 -> 用户 -> 子恢复链路

### Phase I4：A2A 对齐

* 远端事件到统一事件映射
* 远端等待输入到统一状态映射
* 统一错误码

### Phase I5：Checkpoint

* `resume_token`
* 基础 checkpoint 存储
* 故障恢复测试

---

## 十九、可直接并入主文档的精简条文版

1. 长时子 Agent 必须使用统一 `SubAgentStatus` 状态机，而非仅有 completed/failed 二值结果。
2. 父子长时交互必须通过结构化 `DelegationEvent` 通道进行，不得通过共享 session 或 EventBus 充当真相源。
3. 子 Agent 可进入 `WAITING_PARENT`、`WAITING_USER`、`SUSPENDED` 状态，以支持分阶段交互与挂起恢复。
4. `DelegationExecutor` 必须是 spawn / resume / cancel / local-vs-A2A 路由的统一入口。
5. 本地 subagent 与远端 A2A 在框架内必须统一映射到相同的状态、事件与结果语义。
6. 父上下文只允许消费子 Agent 的摘要事件，不得注入子全量 session、tool trace 或原始协议体。
7. 子 Agent 请求用户输入时，必须经过父协调链，形成 `HITLRequest -> HITLResponse -> resume_subagent()` 的闭环。
8. 长时子 Agent 的中间事件与结果提交必须经过统一提交链，保证顺序稳定、可审计、可追踪。
9. 子 Agent 默认仍不得直接写父 SessionState、父 TodoState、父 MemoryStore。
10. 若支持恢复，必须至少提供 `resume_token` 与最小 checkpoint 能力；在未引入 checkpoint 前，不得宣称通用恢复能力。

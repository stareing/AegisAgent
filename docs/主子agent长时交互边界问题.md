这版方案主干是稳的，尤其这几条抓得很准：

* 把**父决策 / 子执行**钉死
* 把**统一提交链**钉死
* 把**子不是父状态真相源**钉死
* 把**本地 subagent / A2A 语义统一**钉死

真正容易出问题的，不是主流程，而是**边界收缩不够严格时的实现漂移**。下面按“最容易失控的边界”来分析。

---

# 一、最大的边界风险：你定义的是“长时子任务”，但实现很容易滑向“第二套 run 系统”

这是第一风险。

你当前方案已经有这些组件：

* `RunCoordinator`
* `RunStateController`
* `CommitSequencer`
* `DelegationExecutor`
* `SubAgentRuntime`
* `SubAgentScheduler`
* `InteractionChannel`
* checkpoint / HITL / background notification

如果不收口，工程实现会自然演化成：

* 父 run 一套生命周期
* 子 run 再一套生命周期
* 背景任务一套通知系统
* HITL 一套等待系统
* A2A 再一套远端状态映射系统

最后变成**4 套半并行状态机叠加**。

## 具体表现

最常见的漂移是：

1. `SubAgentRuntime` 自己维护完整状态机
2. `SubAgentScheduler` 再维护 active map
3. `InteractionChannel` 再维护事件 ack / 游标
4. `RunStateController` 父侧再投影一份摘要状态
5. A2A adapter 再保留一份远端镜像状态

于是“谁是真相源”虽然文档里写了，但代码里会出现多份事实副本。

## 建议补强

应该明确加一条：

> **长时子任务不是独立 run 真相系统，而是父 run 下受控的 delegation object。**

也就是说：

* 子 Agent 可以有执行态
* 可以有事件流
* 可以有 checkpoint
* 但**框架层不应把它提升成与父 run 对等的 RunState 真相实体**

否则你会在后面被迫回答这些难题：

* 子任务是否也有自己的 `RunStateController`？
* 子任务是否也进入 `CommitSequencer`？
* 子任务失败是否算父 run failed？
* 子任务自己的 todo / memory / tool history 是否要持久化成一级对象？

这些问题一旦答“是”，系统就开始失去边界。

---

# 二、状态机边界的核心问题：`WAITING_PARENT`、`WAITING_USER`、`SUSPENDED` 三者有重叠

你现在的状态设计可用，但语义上还不够“正交”。

## 当前问题

这三个状态实际混杂了两类维度：

### 第一类：阻塞来源

* 等父补充输入
* 等用户确认
* 等外部事件

### 第二类：执行控制态

* 已暂停
* 可恢复
* 正在恢复

所以目前会出现模糊区：

* `WAITING_USER` 是否一定也是 suspended？
* `WAITING_PARENT` 是否允许继续后台跑别的步骤？
* `SUSPENDED` 和 `CHECKPOINT_PAUSE` 是不是同一件事？
* `WAIT_EXTERNAL_EVENT` 为什么体现在 suspend reason，而不是 status？

## 建议

更稳的做法是把它拆成**主状态 + 阻塞原因**。

例如：

### 主状态

* `PENDING`
* `RUNNING`
* `PAUSED`
* `RESUMING`
* `COMPLETED`
* `FAILED`
* `CANCELLED`
* `TIMEOUT`

### 阻塞原因

* `NONE`
* `WAIT_PARENT_INPUT`
* `WAIT_USER_INPUT`
* `WAIT_EXTERNAL_EVENT`
* `CHECKPOINT_PAUSE`
* `QUOTA_BACKPRESSURE`
* `MANUAL_REVIEW`

这样好处很大：

1. 避免状态爆炸
2. 避免 `WAITING_USER` / `SUSPENDED` 双重表达
3. A2A 映射更容易
4. UI 和调度都更清晰

如果你不想大改，也至少建议在文档里补一句：

> `WAITING_PARENT`、`WAITING_USER`、`SUSPENDED` 均属于“非运行中可恢复态”，其中等待态是否已真正释放执行资源，必须由 runtime 明确定义，不能靠状态名推断。

否则实现时不同人会有不同理解。

---

# 三、`NON_BLOCKING` / `INTERACTIVE` 的边界还不够清晰

这是第二个很容易失真的点。

你现在定义：

* `BLOCKING`
* `NON_BLOCKING`
* `INTERACTIVE`

但这三个维度其实不是一个轴上的东西。

## 问题本质

* `BLOCKING / NON_BLOCKING` 是**调用返回策略**
* `INTERACTIVE` 是**执行期间是否允许事件交换**

所以 `INTERACTIVE` 不该和前两者并列。

因为现实中完全可能有：

* blocking + interactive
* non-blocking + interactive
* blocking + non-interactive
* non-blocking + non-interactive

## 更合理的拆法

建议拆成两个字段：

```python
wait_mode: Literal["blocking", "non_blocking"]
interaction_mode: Literal["one_shot", "interactive"]
```

或者：

```python
return_mode: Literal["WAIT", "DETACH"]
allow_intermediate_events: bool = False
```

否则你后面一定会碰到这个问题：

> `INTERACTIVE` 到底是阻塞等，还是先返回 handle？

文档现在没把这个边界说死。

---

# 四、事件通道的边界：append-only 对，但 ack 语义还不够清楚

你已经强调：

* append-only
* sequence_no 单调递增
* 不能从文本重建事件

这很好。

但 `ack_event()` 这里有个边界风险：**ack 是谁的 ack？用于什么？**

## 三种不同 ack 语义不能混

现实里 ack 可能代表三件不同的事：

1. **传输确认**
   父已经收到事件

2. **业务消费确认**
   父已经把事件摘要投影进上下文或状态

3. **处理完成确认**
   父已经对该事件做出决策，比如已回复 HITL

如果不拆清楚，后面会产生这些错误：

* progress 被重复注入上下文
* question 被收到但没处理，却被误认为已完成
* 父 crash 后恢复，不知道哪些事件只是拉取了，哪些真正消化了

## 建议

至少补一句：

> `ack_event` 仅表示“父侧已持久化接收”，不得等同于“父已完成业务处理”。

更完整一点的话，可以分成：

* receive_cursor
* projected_cursor
* handled_cursor

哪怕代码里先不做三游标，文档里也要把这个边界讲清楚，否则非常容易实现歪。

---

# 五、父上下文摘要投影边界：现在“可摘要什么”还不够严格

你已经禁止：

* 子全量 session
* 原始工具调用流
* 原始错误栈
* A2A 原始协议包

方向正确，但还缺一个关键边界：

> **摘要是给父 agent 决策用，还是给用户可见叙事用？**

这两者不能混成一个对象。

## 为什么

同一条子事件：

* 对父决策，可能需要结构化信息
  例如：`checkpoint_id`, `progress_percent`, `request_id`, `allowed_options`

* 对用户展示，只需要自然语言
  例如：“代码扫描完成，接下来需要确认是否修改生产配置。”

如果用一个 `DelegationEventSummary` 同时承担两件事，后面一定会出现：

* 为了喂父 LLM，把 UI 噪声带进去
* 为了给用户看，把内部控制字段暴露出去

## 建议

拆成两个投影层：

### 决策投影

给父 agent / coordinator 用的结构化摘要

### 叙事投影

给用户或 session narration 用的自然语言摘要

这会让边界清很多。

---

# 六、HITL 边界是对的，但“谁持有待答请求”还需要钉死

你已经定义：

* 子不能直接问用户
* 子 -> 父
* 父 -> 用户
* 用户 -> 父
* 父 resume 子

这是对的。

但还差一个关键所有权问题：

> **未完成的 HITLRequest 是挂在父 run 上，还是挂在 subagent 上？**

这个必须定死。

## 推荐答案

应当挂在**父 run 控制面**，并保留 `spawn_id` 引用，而不是挂在子 session 里。

原因：

1. 用户只看得到父会话，不看子会话
2. 子可能取消、失败、替换，但父 run 仍需解释待处理请求
3. 同一父 run 可能同时有多个 pending HITL，需要统一仲裁
4. HITL 本质是“父与用户的交互债务”，不是子自己的私有状态

## 需要补的规则

建议补一条：

> `HITLRequest` 的待处理队列属于父 run 控制面；subagent 仅可通过事件提出请求，不拥有面向用户的待处理请求真相表。

这条很重要。

---

# 七、恢复语义边界：现在是“子恢复”，还是“父 run 恢复对子恢复的处理”？

你已经说：

* 先不宣称通用 run-level resume
* 只保证 subagent 交互式恢复

这很对。

但这里要再收紧一个边界：

## 两种恢复不能混

### 1. 执行恢复

子运行到一半停了，之后继续执行

### 2. 协调恢复

父 run 在下一轮重新拿到上下文，并重新决定调用 `resume_subagent()`

实际上用户看到的是第 2 种，runtime 需要支持的是第 1 种。

如果文档不区分，后面会出现假恢复：

* 子其实没保存可恢复状态
* 只是父重新发了一遍任务
* 却对外号称“resume”

## 建议

文档里再加一条非常关键的限制：

> 只有当 runtime 能以 `resume_token` 继续既有执行阶段，而非重新构造一个新任务时，才可称为 `resume`；否则只能称为 `restart-from-checkpoint` 或 `re-dispatch`.

这条可以防止能力表述漂移。

---

# 八、checkpoint 边界：当前容易被误解成“有 token 就等于可恢复”

不是的。

## 核心问题

`resume_token` 只是一个句柄，不代表真正可恢复。

真正的恢复至少还依赖：

* 执行阶段位置
* 最小输入上下文
* 工具副作用是否已提交
* 未完成操作是否幂等
* 外部资源句柄是否还有效

## 典型风险

例如子 Agent 做了：

1. 下载 100 个文件
2. 编辑了 30 个
3. 等用户确认再提交剩余 70 个

如果只保存 `resume_token`，但没保存：

* 哪 30 个已改
* 哪 70 个未改
* 当前 patch set id
* 外部工作目录 / branch / sandbox id

那恢复其实不成立。

## 建议

checkpoint 最少要区分两层：

### 协调层 checkpoint

父知道子停在什么阶段，知道该用哪个 token 恢复

### 执行层 checkpoint

子自己的 runtime 能恢复到哪个安全点

文档里应补一句：

> `resume_token` 仅是恢复入口标识，不等价于执行状态完整快照；是否可恢复取决于 runtime 声明的 checkpoint level。

甚至可以显式定义：

```python
checkpoint_level: Literal[
  "NONE",
  "COORDINATION_ONLY",
  "PHASE_RESTARTABLE",
  "STEP_RESUMABLE"
]
```

这个很有价值。

---

# 九、取消语义边界：`cancel` 是请求，不一定是即时终止

现在文档里有：

* `cancel(spawn_id)`
* `cancel_all(parent_run_id)`

但没有定义取消是：

* best-effort
* cooperative
* preemptive

对于长时 agent，这个边界很重要。

## 为什么

很多工具调用、远端 A2A 请求、shell 命令、外部服务任务，都不能保证即时硬中断。

如果你不写清楚，产品层和调用方会默认：

> 调了 cancel，就已经停了

这会导致后续一堆一致性问题。

## 建议

明确：

> `cancel` 默认为协作式取消请求。进入 `CANCELLED` 之前，runtime 可能经历短暂 `RUNNING` 或 `CANCELLING` 过程；在不可抢占操作中，取消结果不保证即时生效。

最好加一个中间态：

* `CANCELLING`

否则 run 可见语义会过于乐观。

---

# 十、A2A 统一语义没问题，但“最低兼容语义”需要定义

你现在要求本地和 A2A 映射到统一：

* status
* event
* result
* HITL

方向是对的，但还差一个现实问题：

> 远端代理未必支持你定义的全部状态语义。

例如某些远端只有：

* queued
* running
* requires_input
* done
* error

根本没有：

* checkpoint
* suspended
* resumable token
* structured confirmation request

## 建议

定义一个**最低兼容语义层**：

### 必选

* `PENDING`
* `RUNNING`
* `COMPLETED`
* `FAILED`
* `CANCELLED`
* `PROGRESS`
* `QUESTION` 或 `REQUIRES_INPUT`

### 可选增强

* `SUSPENDED`
* `CHECKPOINT`
* `ARTIFACT_READY`
* `RESUMED`
* step-level progress
* typed confirmation

然后要求 A2A adapter 必须声明 capability，而不是默默装作都支持。

比如：

```python
class DelegationCapabilities(BaseModel):
    supports_progress_events: bool = True
    supports_typed_questions: bool = False
    supports_suspend_resume: bool = False
    supports_checkpointing: bool = False
    supports_artifact_streaming: bool = False
```

这会让边界非常清楚。

---

# 十一、背景通知与 delegation event 的边界：现在有收敛机会，但别过度合并

你这里提出：

* 子 Agent 事件 -> background notifications
* 用 `RuntimeNotification(notification_type=["background_task","delegation_event"])`

这是个好方向，但有个边界要小心：

## 不要把二者完全等同

因为它们本质不同：

### background task

更偏系统外部任务完成通知

### delegation event

是父子协作协议的一部分，带有强关联的 `spawn_id / sequence_no / request_id`

如果统一到一个过于通用的 notification 模型，后面容易丢掉 delegation 的严格时序要求。

## 推荐做法

可以统一外壳，不统一内核。

也就是：

* 外层统一进入“runtime notification injection pipeline”
* 内层仍保留不同 payload contract

换句话说：

> 可以共用投递通道，不能共用 delegation 语义模型。

---

# 十二、并发边界：多子任务同时 interactive 时，父 agent 的仲裁规则还缺失

这是很关键的一块。

你已经提到：

* 父可同时管理多个长时子任务

但还没定义父面对多个 active child 时的决策规则。

## 会出现的问题

### 1. 多个子同时发 question

父一次能问用户几个？
是否允许并发挂出多个确认框？

### 2. 多个子同时 completed / failed

父下一轮上下文里按什么顺序注入摘要？

### 3. 一个子等待用户，一个子继续跑

父 run 是否可继续推进另一个子？
还是整个 run 被 pending HITL 卡住？

### 4. 用户回答 A 子的问题时，B 子又发来更高优先级确认

如何抢占？

## 建议补规则

至少要有三条：

### 规则 1：父 run 的 pending HITL 数量受控

这个你已有配置，但建议明确：

* 超过阈值后，新请求排队或降级为父内部决策

### 规则 2：同一时刻默认只暴露一个 foreground HITL

其他请求进入 backlog

### 规则 3：事件注入按 `(priority, created_at, sequence_no)` 排序

不能只按到达时间，否则 A2A 网络抖动会扰乱叙事

---

# 十三、工具副作用边界：子事件与正式提交之间，还缺“副作用已落地”语义

这是实现时特别容易出事故的一点。

## 典型场景

子 Agent 说：

* `ARTIFACT_READY`
* `CHECKPOINT`
* `COMPLETED`

但实际 artifact 还没持久化成功，或者变更还没经过统一提交链。

如果父过早消费这些事件，会产生：

* 父上下文认为文件已生成，实际没有
* 父认为 patch 已存在，实际没提交
* UI 展示“已完成”，但持久层没落地

## 建议

对事件做一个区分：

### 观察性事件

只表示执行中观察到的情况

* `PROGRESS`
* `QUESTION`
* `SUSPENDED`

### 提交后事件

只有在正式提交链确认后才允许发

* `ARTIFACT_READY`
* `COMPLETED` 中引用的产物
* 可见摘要中的变更结论

或者至少明确：

> 子 runtime 发出的事件不自动等价于正式对外可见结果；凡涉及父上下文真相、产物引用、用户可见完成态的内容，必须经过统一提交链确认后才可投影。

这条非常必要。

---

# 十四、`SubAgentResult` 边界：现在有点把“过程态回包”和“终态结果”混在一起了

你已经意识到这一点，所以把 suspend 也放进 `SubAgentResult`。

这是务实的，但语义上容易造成误用。

## 问题

名字叫 `SubAgentResult`，调用方天然会当成“结束结果”。

但你现在让它也表达：

* WAITING_PARENT
* WAITING_USER
* SUSPENDED

也就是过程性回包。

## 更稳的做法

拆成：

### `SubAgentOutcome`

只表示终态：

* completed
* failed
* cancelled
* timeout
* degraded

### `SubAgentContinuation`

表示可继续态：

* waiting_parent
* waiting_user
* suspended

或者更简单：

```python
class DelegationResponse(BaseModel):
    spawn_id: str
    status: SubAgentStatus
    outcome: SubAgentResult | None = None
    continuation: SubAgentSuspendInfo | None = None
```

这样边界更清楚。

---

# 十五、父 run 生命周期边界：子任务比父 run 活得久时怎么办？

这是长时机制里最根本的边界题之一。

## 必须回答的问题

如果父 run 已结束，而子任务仍然：

* 还在跑
* 正在等用户
* 正在等外部事件
* 后续还会发 completed

那这些事件归谁接？

## 三种可能策略

### 策略 A：子必须绑定父 run 生命周期

父 run 结束，所有子都 cancel

优点：简单
缺点：不支持真正长时

### 策略 B：父 run 结束，但子转入系统后台托管

下一轮通过 notification 重新挂回

优点：支持长时
缺点：需要 delegation ownership 迁移语义

### 策略 C：interactive 子不允许超出父 run 生命周期

只有 non-blocking background 子允许脱离

这个通常最稳。

## 建议

文档里至少要补：

> 若子任务生命周期可能长于发起它的父 run，则必须定义其托管者（owner）从“父 run 执行栈”转移到“run-level runtime control plane”；否则不得允许 detached interactive child 持续存活。

这条如果不写，后面会很乱。

---

# 十六、降级 `DEGRADED` 的边界还不够清楚

`DEGRADED` 是有价值的状态，但目前语义太宽。

## 它可能意味着

* 权限不足，退化成只读分析
* quota 紧张，跳过深度步骤
* A2A 能力不足，退化成 one-shot
* 无 checkpoint 能力，退化成 restart only
* 工具不可用，退化成人工确认

这些不是一回事。

## 建议

`DEGRADED` 更适合作为**运行标签**或 result flag，而不是主终态之一。

例如：

```python
degradation_reason: Literal[
  "READ_ONLY_FALLBACK",
  "NO_INTERACTIVE_SUPPORT",
  "QUOTA_LIMITED",
  "NO_RESUME_CAPABILITY",
  "TOOL_UNAVAILABLE"
] | None
```

否则“DEGRADED 是终态还是运行中间态”会变得不清。

---

# 十七、文档条文里建议再补三条硬约束

这是我觉得最值得直接并入主文档的三条。

## 条文 A：子任务不得拥有父级面向用户的待处理交互真相

> 子 Agent 可提出 `QUESTION` / `CONFIRMATION_REQUEST` 事件，但所有待处理的用户交互请求必须登记在父 run 控制面，不得保存在子 session 作为唯一真相源。

## 条文 B：恢复必须区分“继续执行”与“从检查点重启”

> 仅当 runtime 能基于既有执行态继续推进时，方可称为 `resume`；若只能利用阶段摘要或 checkpoint 重新发起后续执行，应视为 `restart-from-checkpoint`，不得与真正恢复混同。

## 条文 C：取消默认是协作式请求

> `cancel` 默认为协作式取消请求而非即时强制终止；进入最终 `CANCELLED` 之前，runtime 可存在短暂过渡态，且对不可抢占操作不保证即时生效。

---

# 十八、总体结论

我对这版方案的判断是：

## 方向正确

它已经把最危险的架构问题压住了：

* 没让子成为父真相源
* 没让父子共享可写状态
* 没让 EventBus 变成事实系统
* 没让 A2A 把内部语义冲散

## 真正要补的是“边界语义收紧”

最需要补强的不是再加模块，而是把这几类边界钉死：

1. **子不是第二套 run 系统**
2. **状态机要区分主状态与阻塞原因**
3. **interactive 和 blocking 不是同一维度**
4. **ack 不等于已处理**
5. **HITL 待答请求归父 control plane**
6. **resume 不等于 restart**
7. **cancel 默认协作式**
8. **父 run 结束后子任务归属要定义**
9. **A2A 必须声明 capability，不可假装完全对齐**
10. **提交后可见结果与观察性事件必须分层**


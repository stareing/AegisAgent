# AgentBus 控制总线设计文档

**版本**：v1.0
**状态**：Draft
**前置文档**：`Agent Team 协作协议.md`（事件邮箱协议 v1.0）
**目标**：为 Agent Team 自由交互提供统一控制总线，替代现有 5 条独立事件通路

---

## 一、背景与动机

### 1.1 现状

当前框架存在 5 条独立的事件/消息通路：

| 通路 | 位置 | 方向 | 持久化 | 寻址方式 |
|------|------|------|--------|---------|
| EventBus | `infra/event_bus.py` | 发布/订阅 | 无 | event_name 字符串 |
| InteractionChannel | `subagent/interaction_channel.py` | 子→父 | 可选 SQLite | spawn_id |
| SiblingChannel | `subagent/sibling_channel.py` | 兄弟↔兄弟 | 无 | (parent_run_id, spawn_id) |
| BackgroundNotifier | `notification/background.py` | 系统→父 | 无 | task_id |
| RuntimeNotificationChannel | `notification/channel.py` | 统一排空 | 无 | spawn_id |

### 1.2 问题

1. **寻址不统一** — 每条通路用不同 ID 体系，无法跨通路路由
2. **无广播** — 不能向"所有子代理"或"同组代理"广播
3. **无订阅过滤** — 消费者只能全量拉取
4. **无跨层路由** — 子代理事件不能触发 hook，hook 不能向子代理推送
5. **通路互不感知** — 无法组合使用
6. **不支持 Team 协作协议** — `Agent Team 协作协议.md` 定义的 16 种事件类型无法在现有通路上实现

### 1.3 目标

构建 **AgentBus** 统一控制总线：

- 统一寻址（BusAddress）
- 统一信封（BusEnvelope）
- 结构化 Topic 路由
- 支持单播/广播/组播
- 支持请求/响应模式
- 支持订阅过滤
- 可选持久化（内存 / SQLite）
- 桥接现有 5 条通路（叠加层，不替代）
- 完整支撑 `Agent Team 协作协议.md` 定义的所有交互模式

---

## 二、核心原则

### 2.1 总线只是投递通道

总线只负责路由和投递，不负责业务语义解释。与 `Agent Team 协作协议.md` §2.3 一致：

> 邮箱不是状态真相源。正式状态必须分别存放在对应存储中。

总线不替代 Team Registry、Task Store、Plan Registry、Shutdown Registry。

### 2.2 先写状态，再发事件

与 `Agent Team 协作协议.md` §2.4 一致：

> 先写正式状态存储，再发送邮箱事件。

AgentBus 的 `publish()` 不保证投递顺序等于业务因果顺序。消费者必须通过 `correlation_id` 和正式状态存储验证因果关系。

### 2.3 信封不可变

`BusEnvelope` 是 `frozen=True` 的 Pydantic 模型。一旦发布，任何消费者不可修改信封内容。

### 2.4 消费者异常不传播

总线投递过程中，如果某个订阅者抛出异常，不得传播到发布方。与 EventBus §49 一致。

### 2.5 叠加层而非替代层

AgentBus 桥接现有通路，不强制替换。现有 InteractionChannel / SiblingChannel / BackgroundNotifier 仍可直接使用。AgentBus 提供统一视图和路由能力。

### 2.6 结构化事件，非聊天

与 `Agent Team 协作协议.md` §2.2 一致：

> 邮箱中出现的每一条内容，都必须是可解释、可追踪、可审计的结构化事件。

自然语言只能出现在 `payload.message` 等辅助字段中。

---

## 三、数据模型

### 3.1 BusAddress — 统一寻址

```python
class BusAddress(BaseModel, frozen=True):
    """总线上的参与者地址。

    寻址层级: agent_id (必须) > run_id (可选) > role (可选) > group (可选)

    约束:
    - agent_id 必须非空
    - group 用于组播，空字符串表示不属于任何组
    - "system" 是保留 agent_id，代表框架本身
    """

    agent_id: str              # 代理标识 (spawn_id / parent_id / "system")
    run_id: str = ""           # 运行上下文 (parent_run_id)
    role: str = ""             # 角色标签 (如 "researcher", "reviewer")
    group: str = ""            # 组标签 (如 "team_alpha", team_id)
```

#### 地址匹配规则

| 发送目标 | 匹配条件 | 说明 |
|---------|---------|------|
| `BusAddress(agent_id="agent_A")` | `agent_id == "agent_A"` | 精确单播 |
| `BusAddress(agent_id="*", group="team_1")` | `group == "team_1"` | 组播 |
| `BusAddress(agent_id="*")` | 所有注册参与者 | 全局广播 |
| `target=None` | 仅订阅者匹配 topic | 发布/订阅模式 |

#### 保留地址

| agent_id | 含义 |
|---------|------|
| `"system"` | 框架系统（后台任务通知、hook 触发等） |
| `"*"` | 通配符（用于广播/组播的 target） |
| `"lead"` | 当前 Team 的 Lead Agent（语义别名） |

### 3.2 BusEnvelope — 统一信封

```python
class BusEnvelope(BaseModel, frozen=True):
    """总线消息的统一信封。不可变。

    信封 = 路由信息 + 业务载荷。
    总线只读路由信息，不解释 payload。
    """

    # 标识
    envelope_id: str           # 全局唯一 (env_{uuid12})

    # 路由
    topic: str                 # 路由键，dot-separated (如 "agent.progress")
    source: BusAddress         # 发送方地址
    target: BusAddress | None  # 接收方地址 (None = 仅通过 topic 订阅匹配)

    # 载荷
    payload: dict              # 业务数据，格式由 topic 约定

    # 元数据
    created_at: datetime       # UTC
    correlation_id: str = ""   # 因果关联 ID (请求-响应追踪)
    reply_to: str = ""         # 回复目标 envelope_id
    ttl_ms: int = 0            # 存活时间 (0 = 永不过期)
    priority: int = 5          # 0(最高) ~ 9(最低)
    requires_ack: bool = False # 是否要求接收确认
    ack_level: AckLevel = AckLevel.NONE  # 当前确认级别

    # 扩展
    metadata: dict = {}        # 框架扩展字段 (不进入路由逻辑)
```

#### 信封字段约束

| 字段 | 约束 |
|------|------|
| `envelope_id` | 必须全局唯一，格式 `env_{uuid.hex[:12]}` |
| `topic` | 必须非空，dot-separated，最多 5 层 |
| `source` | 必须非空（匿名发送不允许） |
| `target` | None 表示仅通过 topic 路由（pub/sub 模式） |
| `payload` | 必须是 dict，总线不校验内容 |
| `correlation_id` | 请求/响应场景必须设置 |
| `reply_to` | 仅回复消息设置，指向原始 envelope_id |
| `ttl_ms` | 0 表示永不过期。过期信封被清理线程移除 |
| `priority` | 0 最高，9 最低。影响消费顺序，不影响投递 |
| `ack_level` | 只能单调递增：NONE → RECEIVED → PROJECTED → HANDLED |

### 3.3 Topic 体系

#### Topic 命名规范

```
{domain}.{entity}.{action}
```

| 层级 | 说明 | 示例 |
|------|------|------|
| domain | 事件领域 | `agent`, `team`, `system`, `task` |
| entity | 事件实体 | agent_id, group_id, `*` |
| action | 事件动作 | `progress`, `question`, `completed` |

#### 预定义 Topic

```python
# ── Agent 生命周期 ──
TOPIC_AGENT_STARTED     = "agent.{agent_id}.started"
TOPIC_AGENT_PROGRESS    = "agent.{agent_id}.progress"
TOPIC_AGENT_QUESTION    = "agent.{agent_id}.question"
TOPIC_AGENT_ANSWER      = "agent.{agent_id}.answer"
TOPIC_AGENT_COMPLETED   = "agent.{agent_id}.completed"
TOPIC_AGENT_FAILED      = "agent.{agent_id}.failed"
TOPIC_AGENT_SUSPENDED   = "agent.{agent_id}.suspended"
TOPIC_AGENT_RESUMED     = "agent.{agent_id}.resumed"

# ── Team 协作 (对应 Agent Team 协作协议 §6) ──
TOPIC_TASK_ASSIGNMENT    = "task.{task_id}.assignment"
TOPIC_TASK_CLAIM         = "task.{task_id}.claim_request"
TOPIC_TASK_CLAIMED       = "task.{task_id}.claimed"
TOPIC_TASK_HANDOFF       = "task.{task_id}.handoff_request"
TOPIC_TASK_HANDOFF_RESP  = "task.{task_id}.handoff_response"
TOPIC_PLAN_SUBMISSION    = "plan.{request_id}.submission"
TOPIC_PLAN_APPROVAL      = "plan.{request_id}.approval"
TOPIC_SHUTDOWN_REQUEST   = "team.{team_id}.shutdown_request"
TOPIC_SHUTDOWN_ACK       = "team.{team_id}.shutdown_ack"
TOPIC_STATUS_PING        = "agent.{agent_id}.status_ping"
TOPIC_STATUS_REPLY       = "agent.{agent_id}.status_reply"
TOPIC_ERROR_NOTICE       = "agent.{agent_id}.error"

# ── Team 广播 ──
TOPIC_TEAM_BROADCAST     = "team.{group}.broadcast"
TOPIC_TEAM_COORDINATION  = "team.{group}.coordination"

# ── 系统事件 ──
TOPIC_SYSTEM_TASK_DONE   = "system.task.completed"        # 后台 bash 任务
TOPIC_SYSTEM_CHECKPOINT  = "system.checkpoint.saved"      # 快照保存
TOPIC_SYSTEM_HOOK        = "system.hook.{hook_point}"     # Hook 触发
```

#### Topic 通配符订阅

| 模式 | 匹配 | 示例 |
|------|------|------|
| `agent.*.progress` | 所有代理的进度事件 | 父代理监听所有子代理 |
| `task.*.*` | 所有任务相关事件 | Lead 监听所有任务变化 |
| `team.team_1.*` | team_1 的所有团队事件 | 组内成员监听广播 |
| `*` | 所有事件 | 调试/审计 |

通配符规则：
- `*` 匹配单层中的任意内容
- 仅支持 `*` 单字符通配，不支持 `**` 多层通配
- 通配符只在订阅端生效，发布端必须使用完整 topic

---

## 四、AgentBus API

### 4.1 核心接口

```python
class AgentBusProtocol(Protocol):
    """Agent Team 统一控制总线协议。

    职责边界:
    ✅ 统一寻址和路由
    ✅ Topic 订阅/发布
    ✅ 广播/组播/单播
    ✅ 持久化投递保证 (可选)
    ✅ 事件关联追踪

    ❌ 业务逻辑解释 (消费者自行解释 payload)
    ❌ 状态管理 (不存储代理状态)
    ❌ 策略决策 (路由规则由 Topic 定义)
    ❌ 替代正式状态存储 (Team Registry / Task Store)
    """

    # ── 发布 ──
    def publish(self, envelope: BusEnvelope) -> None: ...
    def send(self, topic: str, payload: dict, source: BusAddress,
             target: BusAddress, correlation_id: str = "") -> BusEnvelope: ...
    def broadcast(self, topic: str, payload: dict, source: BusAddress,
                  group: str = "") -> BusEnvelope: ...
    def reply(self, original: BusEnvelope, payload: dict,
              source: BusAddress) -> BusEnvelope: ...

    # ── 订阅 (推模式) ──
    def subscribe(self, topic_pattern: str, handler: BusHandler,
                  filter: SubscriptionFilter | None = None) -> str: ...
    def unsubscribe(self, subscription_id: str) -> None: ...

    # ── 拉取 (轮询模式) ──
    def drain(self, address: BusAddress,
              topic_pattern: str = "*") -> list[BusEnvelope]: ...
    def peek(self, address: BusAddress,
             topic_pattern: str = "*") -> list[BusEnvelope]: ...
    def ack(self, envelope_id: str, level: AckLevel) -> None: ...

    # ── 查询 ──
    def pending_count(self, address: BusAddress) -> int: ...
    def list_participants(self, group: str = "") -> list[BusAddress]: ...

    # ── 生命周期 ──
    def register_participant(self, address: BusAddress) -> None: ...
    def unregister_participant(self, address: BusAddress) -> None: ...
    def clear_group(self, group: str) -> int: ...
    def shutdown(self) -> None: ...
```

### 4.2 BusHandler — 订阅回调

```python
class BusHandler(Protocol):
    """订阅回调协议。同步或异步。"""

    def __call__(self, envelope: BusEnvelope) -> None: ...
```

### 4.3 SubscriptionFilter — 订阅过滤器

```python
class SubscriptionFilter(BaseModel):
    """订阅时的过滤条件。所有条件 AND 组合。

    空列表 / 默认值 = 不过滤。
    """

    source_agent_ids: list[str] = []     # 只接收特定来源
    exclude_agent_ids: list[str] = []    # 排除特定来源
    groups: list[str] = []               # 只接收特定组
    min_priority: int = 0                # 优先级下限 (含)
    max_priority: int = 9                # 优先级上限 (含)
    requires_ack_only: bool = False      # 只要需要确认的
    payload_contains: dict = {}          # payload 必须包含指定键值对
```

---

## 五、投递语义

### 5.1 三种投递模式

| 模式 | 方法 | target | 投递对象 |
|------|------|--------|---------|
| 单播 | `send()` | 指定 BusAddress | 精确一个参与者 |
| 组播 | `broadcast(group=X)` | `agent_id="*", group=X` | 组内所有参与者 |
| 发布/订阅 | `publish()` | None | topic 匹配的所有订阅者 |

### 5.2 投递顺序保证

| 保证 | 范围 | 说明 |
|------|------|------|
| 同源有序 | 同一 source → 同一 target | 按 `created_at` 顺序投递 |
| 跨源无序 | 不同 source → 同一 target | 无顺序保证 |
| 广播无序 | 同一 source → 多个 target | 各 target 收到顺序可能不同 |

### 5.3 确认机制 (AckLevel)

复用现有 `AckLevel` 四级确认模型：

```
NONE → RECEIVED → PROJECTED → HANDLED
```

| 级别 | 含义 | 设置者 |
|------|------|--------|
| NONE | 信封刚发布，尚未被消费 | 初始状态 |
| RECEIVED | 目标方基础设施已接收 | `drain()` 自动设置 |
| PROJECTED | 目标方已将事件纳入决策上下文 | 消费者显式调用 `ack()` |
| HANDLED | 目标方已完成业务处理 | 消费者显式调用 `ack()` |

只能单调递增，不可回退。

### 5.4 TTL 过期

- `ttl_ms > 0` 的信封在 `created_at + ttl_ms` 后被视为过期
- 过期信封不再投递给新的 `drain()` 调用
- 过期信封由 `cleanup_expired()` 物理删除
- `ttl_ms = 0` 表示永不过期（默认）

---

## 六、持久化

### 6.1 BusPersistence 协议

```python
class BusPersistence(Protocol):
    """总线消息持久化后端。"""

    def store(self, envelope: BusEnvelope) -> None: ...
    def load_inbox(self, address: BusAddress,
                   after_envelope_id: str = "") -> list[BusEnvelope]: ...
    def update_ack(self, envelope_id: str, level: AckLevel) -> None: ...
    def cleanup_expired(self) -> int: ...
    def cleanup_group(self, group: str) -> int: ...
    def get_envelope(self, envelope_id: str) -> BusEnvelope | None: ...
    def close(self) -> None: ...
```

### 6.2 实现策略

| 后端 | 场景 | 特点 |
|------|------|------|
| `InMemoryBusPersistence` | 开发/测试 | 零依赖，进程退出丢失 |
| `SQLiteBusPersistence` | 生产单进程 | crash recovery，TTL 清理 |
| （预留）`RedisBusPersistence` | 多进程部署 | 跨进程广播，pub/sub |

### 6.3 SQLite Schema

```sql
CREATE TABLE IF NOT EXISTS bus_envelopes (
    envelope_id    TEXT PRIMARY KEY,
    topic          TEXT NOT NULL,
    source_json    TEXT NOT NULL,        -- BusAddress JSON
    target_json    TEXT,                 -- BusAddress JSON, NULL = pub/sub
    payload_json   TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    correlation_id TEXT NOT NULL DEFAULT '',
    reply_to       TEXT NOT NULL DEFAULT '',
    ttl_ms         INTEGER NOT NULL DEFAULT 0,
    priority       INTEGER NOT NULL DEFAULT 5,
    requires_ack   INTEGER NOT NULL DEFAULT 0,
    ack_level      TEXT NOT NULL DEFAULT 'NONE',
    metadata_json  TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_bus_target ON bus_envelopes
    (json_extract(target_json, '$.agent_id'), ack_level);
CREATE INDEX IF NOT EXISTS idx_bus_topic ON bus_envelopes (topic);
CREATE INDEX IF NOT EXISTS idx_bus_group ON bus_envelopes
    (json_extract(target_json, '$.group'));
CREATE INDEX IF NOT EXISTS idx_bus_created ON bus_envelopes (created_at);
CREATE INDEX IF NOT EXISTS idx_bus_corr ON bus_envelopes (correlation_id);
```

---

## 七、桥接层

### 7.1 桥接原则

- 现有通路保留不动
- AgentBus 作为上层抽象
- 桥接器将现有事件转换为 BusEnvelope
- 桥接器将 BusEnvelope 转换回现有事件格式
- 双向桥接，不是单向

### 7.2 桥接映射

| 现有组件 | → BusEnvelope | BusEnvelope → | Topic 模式 |
|---------|--------------|---------------|-----------|
| InteractionChannel | `DelegationEvent` → Envelope | Envelope → `DelegationEvent` | `agent.{spawn_id}.{event_type}` |
| SiblingChannel | `SiblingMessage` → Envelope | Envelope → `SiblingMessage` | `agent.{to_spawn_id}.message` |
| BackgroundNotifier | `BackgroundNotification` → Envelope | — (单向) | `system.task.completed` |
| EventBus | `EventEnvelope` → BusEnvelope | — (单向) | `system.event.{event_name}` |
| HookDispatcher | — | BusEnvelope → 触发 hook chain | `system.hook.{hook_point}` |

### 7.3 桥接器接口

```python
class BusBridge(Protocol):
    """单条通路的桥接器。"""

    def attach(self, bus: AgentBus) -> None: ...
    def detach(self) -> None: ...
```

每个桥接器负责：
1. 监听原通路的新事件
2. 转换为 BusEnvelope
3. 调用 `bus.publish()` 投递
4. （可选）监听 bus 上的特定 topic，转换回原通路格式

---

## 八、与 Agent Team 协作协议的对接

### 8.1 事件类型映射

`Agent Team 协作协议.md` §5.1 定义的 16 种 MailEventType 全部映射到 AgentBus Topic：

| MailEventType | BusEnvelope topic | payload 格式 |
|--------------|-------------------|-------------|
| TASK_ASSIGNMENT | `task.{task_id}.assignment` | {task_id, subject, description, priority, message} |
| TASK_CLAIM_REQUEST | `task.{task_id}.claim_request` | {task_id, request_id, message} |
| TASK_CLAIMED_NOTICE | `task.{task_id}.claimed` | {task_id, owner, message} |
| TASK_HANDOFF_REQUEST | `task.{task_id}.handoff_request` | {task_id, request_id, target_role, reason} |
| TASK_HANDOFF_RESPONSE | `task.{task_id}.handoff_response` | {request_id, task_id, accepted, message} |
| PLAN_SUBMISSION | `plan.{request_id}.submission` | {request_id, task_id, title, plan_text, risk_level} |
| APPROVAL_RESPONSE | `plan.{request_id}.approval` | {request_id, approved, feedback} |
| QUESTION | `agent.{from_agent}.question` | {question_id, task_id, question, options} |
| ANSWER | `agent.{to_agent}.answer` | {question_id, task_id, answer} |
| PROGRESS_NOTICE | `agent.{from_agent}.progress` | {task_id, progress_percent, summary} |
| STATUS_PING | `agent.{to_agent}.status_ping` | {request_id} |
| STATUS_REPLY | `agent.{from_agent}.status_reply` | {request_id, status, active_task_ids} |
| SHUTDOWN_REQUEST | `team.{team_id}.shutdown_request` | {request_id, reason} |
| SHUTDOWN_ACK | `team.{team_id}.shutdown_ack` | {request_id, accepted} |
| ERROR_NOTICE | `agent.{from_agent}.error` | {error_code, task_id, summary} |
| BROADCAST_NOTICE | `team.{group}.broadcast` | {topic, summary, message} |

### 8.2 正式状态存储对接

AgentBus 不替代正式状态存储。Team 协作所需的 4 个 Registry 由独立模块实现：

| Registry | 职责 | AgentBus 关系 |
|---------|------|-------------|
| TeamRegistry | 成员状态真相 | Bus 事件触发后读取/验证 |
| TaskStore | 任务状态真相 | 任务状态变更先写 Store，再发 Bus 事件 |
| PlanRegistry | 计划审批真相 | 审批结果先写 Registry，再发 Bus 事件 |
| ShutdownRegistry | 关闭握手真相 | 关闭状态先写 Registry，再发 Bus 事件 |

---

## 九、内置工具

### 9.1 Agent 可用工具

以下内置工具暴露给 Agent LLM 调用：

```python
# 发送消息给指定代理
bus_send(to_agent_id: str, topic: str, payload: dict) -> dict

# 广播消息给整个 team
bus_broadcast(topic: str, payload: dict, group: str = "") -> dict

# 拉取自己的未读消息
bus_drain(topic_pattern: str = "*") -> dict

# 确认消息已处理
bus_ack(envelope_id: str, level: str = "HANDLED") -> dict

# 查看待处理消息数量
bus_pending() -> dict

# 查看团队成员列表
bus_list_team(group: str = "") -> dict
```

### 9.2 工具命名空间

所有 Bus 工具归属 `system` 命名空间，`delegation` 类别：

```python
namespace = SYSTEM_NAMESPACE
source = "bus"
category = "delegation"
```

---

## 十、线程与异步模型

### 10.1 线程安全

- 所有 AgentBus 公共方法必须线程安全
- 使用 `threading.Lock` 保护内部状态（与现有 InteractionChannel 一致）
- 订阅回调在发布者线程中同步执行（与 EventBus 一致）

### 10.2 异步兼容

- `publish()` / `send()` / `broadcast()` 是同步方法（不阻塞）
- `drain()` / `peek()` 是同步方法（立即返回）
- 订阅回调 `BusHandler` 支持同步和异步两种
- 异步回调通过 `asyncio.create_task` 调度，不阻塞发布者

### 10.3 背压

- 每个参与者的收件箱有上限（默认 500 信封）
- 超过上限时，新信封按优先级淘汰最低优先级的旧信封
- TTL 过期的信封由 `drain()` 调用时惰性清理

---

## 十一、设计约束清单

| # | 约束 | 理由 |
|---|------|------|
| 1 | BusEnvelope 不可变 (frozen=True) | 防止消费者篡改共享消息 |
| 2 | Topic 必须 dot-separated，最多 5 层 | 支持通配符订阅，防止过深嵌套 |
| 3 | 同步 publish，异步可选 handler | publish 不阻塞发送方 |
| 4 | 消费者异常不传播到发布方 | 隔离故障域 |
| 5 | TTL 过期由 drain/cleanup 驱动 | 不引入后台定时器 |
| 6 | 单进程内 threading.Lock | 与现有通路模式一致 |
| 7 | drain/peek 返回深拷贝 | 不暴露内部引用 |
| 8 | correlation_id 由调用方设置 | 框架不自动关联因果 |
| 9 | 现有通路可直接使用 | AgentBus 是叠加层 |
| 10 | payload 不做 schema 校验 | 由消费者按 topic 约定验证 |
| 11 | envelope_id 全局唯一 | 支持幂等消费和去重 |
| 12 | ack_level 只能单调递增 | 防止状态回退 |
| 13 | 广播不保证到达顺序 | 各接收方可能不同顺序 |
| 14 | 总线不是状态真相源 | §2.1 核心原则 |
| 15 | 先写状态存储，再发 Bus 事件 | §2.2 核心原则 |

---

## 十二、文件结构

```
agent_framework/notification/
├── __init__.py              # 公共导出
├── bus.py                   # AgentBus 实现 (统一控制总线)
├── envelope.py              # BusEnvelope + BusAddress 模型
├── topics.py                # Topic 常量 + 通配符匹配
├── subscriber.py            # SubscriptionFilter + Subscription 管理
├── persistence.py           # BusPersistence 协议 + InMemory/SQLite 实现
├── bridges/                 # 桥接层
│   ├── __init__.py
│   ├── interaction.py       # InteractionChannel ↔ Bus
│   ├── sibling.py           # SiblingChannel ↔ Bus
│   ├── background.py        # BackgroundNotifier → Bus
│   └── event_bus.py         # EventBus → Bus
├── channel.py               # RuntimeNotificationChannel (保留，接入 bus)
└── background.py            # BackgroundNotifier (保留)
```

---

## 十三、实现阶段

| Phase | 内容 | 交付物 | 依赖 |
|-------|------|--------|------|
| **P0** | 数据模型 | `envelope.py` (BusEnvelope, BusAddress), `topics.py` | 无 |
| **P1** | 核心总线 | `bus.py` (publish/subscribe/drain/ack), `subscriber.py`, `InMemoryBusPersistence` | P0 |
| **P2** | 桥接层 | `bridges/*.py` (4 个桥接器) | P1 |
| **P3** | 持久化 | `persistence.py` (SQLiteBusPersistence + TTL 清理) | P1 |
| **P4** | 内置工具 | `tools/builtin/bus_tools.py` (bus_send/drain/broadcast/ack) | P2 |
| **P5** | Coordinator 集成 | 替代 RuntimeNotificationChannel 直接调用 | P2 |
| **P6** | Team 协作集成 | TeamRegistry/TaskStore/PlanRegistry/ShutdownRegistry + Bus 事件 | P4 |

---

## 十四、验收标准

### P0-P1 验收

- [ ] BusEnvelope 不可变（frozen=True 验证）
- [ ] Topic 通配符匹配正确（`agent.*.progress` 匹配 `agent.A.progress`）
- [ ] 单播投递精确到 agent_id
- [ ] 组播投递到 group 内所有成员
- [ ] pub/sub 模式通过 topic 匹配投递
- [ ] drain() 返回后标记 RECEIVED
- [ ] ack_level 只能单调递增
- [ ] 收件箱上限 + 优先级淘汰
- [ ] 消费者异常不传播
- [ ] 线程安全（并发 publish + drain 无 data race）

### P2 验收

- [ ] DelegationEvent → BusEnvelope 转换无损
- [ ] SiblingMessage → BusEnvelope 转换无损
- [ ] BackgroundNotification → BusEnvelope 转换无损
- [ ] 桥接双向：Bus 上的消息能回写到原通路

### P3 验收

- [ ] SQLite 持久化后 crash + 重启能恢复 pending 信封
- [ ] TTL 过期清理正确
- [ ] 大量信封（10000+）读写性能可接受 (<100ms)

### P4-P6 验收

- [ ] Agent 可通过 bus_send/bus_drain 工具交互
- [ ] Coordinator drain 循环通过 AgentBus 统一拉取
- [ ] Team 协作协议 16 种事件全部通过 Bus 投递
- [ ] 先写 Registry → 再发 Bus 事件的顺序保证
- [ ] 全部现有测试不回退

---

## 十五、与现有文档的关系

| 文档 | 关系 |
|------|------|
| `Agent Team 协作协议.md` | AgentBus 是其 §4.5 Event Mailbox 的实现载体 |
| `ai_agent_framework_v3_optimized.md` §32 | AgentBus 扩展多 Agent 协同的通信层 |
| `CLAUDE.md` 设计原则 | AgentBus 遵循：面向接口编程、显式优于隐式、副作用集中 |

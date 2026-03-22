# Agent Team 差异分析：Claude Code Teams vs 当前框架实现

## 1. 架构对比总览

| 维度 | Claude Code Agent Teams | 当前框架实现 | 差异等级 |
|------|------------------------|-------------|---------|
| **成员独立性** | 每个 teammate 是独立 Claude Code 进程，拥有独立上下文窗口 | teammates 是 ephemeral sub-agent run，共享父进程 | 🔴 核心差异 |
| **通信模型** | teammates 之间直接消息，无需经过 lead | 有 mail() 工具支持点对点/广播/pub-sub | 🟢 已实现 |
| **任务协调** | 共享任务列表，teammates 自主认领 | lead 分配任务，无自主认领 | 🟡 部分缺失 |
| **会话持久性** | 独立会话可恢复，支持长时运行 | 一次性 ephemeral run，completion 即结束 | 🔴 核心差异 |
| **显示模式** | in-process / split-pane (tmux/iTerm2) | 经典 REPL + Textual TUI 后台通知 | 🟡 模式不同 |
| **权限控制** | 继承 lead 权限，可独立调整 | teammate 权限隔离（不可 spawn/shutdown）| 🟢 已实现 |
| **计划审批** | lead 可要求 plan approval before coding | 支持 PLAN_SUBMISSION → approve/reject → 继续 | 🟢 已实现 |
| **关闭管理** | 优雅关闭协议（请求→确认） | SHUTDOWN_REQUEST → SHUTDOWN_ACK 协议 | 🟢 已实现 |
| **Hooks** | TeammateIdle / TaskCompleted 钩子 | 无对应钩子 | 🟡 缺失 |
| **团队配置存储** | `~/.claude/teams/{name}/config.json` | 内存态 TeamRegistry + `.agent-team/` TEAM.md | 🟡 模式不同 |

---

## 2. 详细差异分析

### 2.1 🔴 成员独立性：进程 vs 子任务

**Claude Code 设计**：
- 每个 teammate 是**独立的 Claude Code 进程**
- 拥有完全独立的上下文窗口
- 加载项目上下文（CLAUDE.md、MCP servers、skills）
- 不继承 lead 的对话历史
- 可以被用户直接交互（Shift+Down 或点击 pane）

**当前实现**：
- teammates 是 `SubAgentRuntime.spawn_async(mode=EPHEMERAL)` 的一次性子任务
- 在父进程的事件循环中运行
- 通过 `SubAgentFactory.create_agent_and_deps()` 创建隔离的工具上下文
- 任务完成后 sub-agent 释放，无法持续交互
- 支持 Q&A 周期（QUESTION → ANSWER → continuation run），但每轮是新 run

**影响**：
- 当前 teammates 无法像独立进程一样长时间运行
- 无法由用户直接与 teammate 交互（只能通过 lead 代理）
- Continuation run 通过重建上下文模拟持久会话，但上下文深度有限

**建议**：
- 考虑 `SpawnMode.LONG_LIVED` 替代 `EPHEMERAL`，使用 SubAgent 的 suspend/resume
- 或引入独立进程模型（如 subprocess + socket 通信）

---

### 2.2 🔴 任务列表：共享自认领 vs Lead 分配

**Claude Code 设计**：
- 共享任务列表（`~/.claude/tasks/{team-name}/`）
- 三种状态：pending / in_progress / completed
- 支持任务依赖（依赖完成后自动解锁）
- Teammates **自主认领**下一个未分配的未阻塞任务
- 文件锁防止竞态条件
- Lead 也可显式分配

**当前实现**：
- 无共享任务列表
- Lead 通过 `team(action="assign")` 显式分配
- 无任务依赖图
- 无自主认领机制
- 框架有 `tasks/` 模块（DAG 任务系统），但未接入 team

**影响**：
- Teammates 完成任务后处于 IDLE，需要 lead 再次分配
- 无法利用已有的 DAG 任务系统进行自动依赖解锁
- 增加 lead 编排负担

**建议**：
- 将现有 `agent_framework/tasks/` 模块接入 team 系统
- 添加 `team(action="claim")` 让 teammate 自主认领
- 添加任务依赖字段和自动解锁逻辑

---

### 2.3 🟡 显示模式差异

**Claude Code 设计**：
- **in-process**：所有 teammates 在主终端运行，Shift+Down 切换
- **split-pane**：每个 teammate 独立 pane（tmux/iTerm2）
- 自动检测：tmux 内用 split，否则 in-process
- 用户可**直接与任意 teammate 交互**

**当前实现**：
- 经典 REPL：后台 `_display_team_output()` 轮询显示通知
- Textual TUI：`_display_team_output()` 异步轮询渲染
- `/team-status` / `/team-inbox` 等命令查看状态
- 用户通过 lead 代理所有交互，无法直接操作 teammate

**建议**：
- 当前模式适合"lead 编排"场景
- 若需支持直接交互，需要多终端/多 tab 架构

---

### 2.4 🟡 Hooks 缺失

**Claude Code 设计**：
- `TeammateIdle`：teammate 即将空闲时触发，exit code 2 可阻止并反馈
- `TaskCompleted`：任务标记完成时触发，exit code 2 可阻止并反馈

**当前实现**：
- 框架有完整的 Hook 子系统（`HookPoint` 枚举 + `HookDispatchService`）
- 但无 team 相关 HookPoint

**建议**：
在 `agent_framework/models/hook.py` 的 `HookPoint` 枚举中新增：
```python
TEAMMATE_IDLE = "teammate_idle"
TEAM_TASK_COMPLETED = "team_task_completed"
```

---

### 2.5 🟢 已对齐的能力

| 能力 | Claude Code | 当前实现 |
|------|-------------|---------|
| **消息系统** | 点对点 + broadcast | mail(send/broadcast/reply/publish/subscribe) ✅ |
| **计划审批** | plan approval before implementation | PLAN_SUBMISSION → approve/reject → continuation ✅ |
| **关闭协议** | graceful shutdown request/ack | SHUTDOWN_REQUEST → SHUTDOWN_ACK ✅ |
| **权限隔离** | inherit lead, can change individually | teammate 不可 spawn/shutdown, 可 status ✅ |
| **项目上下文** | CLAUDE.md / skills / MCP | TEAM.md 角色定义 + allowed-tools ✅ |
| **嵌套限制** | teammates cannot spawn sub-teams | teammate 权限拒绝 create/spawn ✅ |
| **自发消息拦截** | N/A | 不可 send to self ✅ |
| **状态感知** | N/A | is_you / your_id / your_role ✅ |

---

## 3. 当前框架独有优势

以下是当前框架有但 Claude Code Agent Teams 未提及的能力：

| 能力 | 描述 |
|------|------|
| **4 种协作模式** | Star / Mesh / Pub-Sub / Request-Reply |
| **Topic pub/sub** | 主题订阅/发布机制（`publish`/`subscribe`/`unsubscribe`）|
| **10 态状态机** | SPAWNING → IDLE → WORKING → RESULT_READY → NOTIFYING → IDLE（含 WAITING_ANSWER/APPROVAL）|
| **结构化通知类型** | TeamNotification 模型（TASK_COMPLETED / TASK_FAILED / QUESTION / PLAN / BROADCAST / ERROR）|
| **通知策略** | TeamNotificationPolicy（可配置哪些事件升级到主模型）|
| **自动通知 turn** | RunDispatcher 后台自动生成 LLM 总结，无需用户询问 |
| **多轮 Q&A** | assign → 提问 → 等答 → continuation → 再提问 → ... → 最终结果（最多 10 轮）|
| **原子占用保护** | assign_task 同步 IDLE→WORKING，防止竞态重复分配 |
| **身份一致性** | spawn_id = member agent_id，mail from_agent = team registry identity |
| **角色定义** | `.agent-team/<role>/TEAM.md` 声明式角色定义（与 SKILL.md 同模式）|
| **配额释放** | sub-agent 完成后释放配额槽位，允许跨轮复用 |

---

## 4. 实施优先级建议

### P0：核心能力对齐（最大用户价值差距）

| # | 改造项 | 当前状态 | 预估 |
|---|--------|---------|------|
| 1 | **共享任务列表 + 自认领** | 无 | 2d |
| 2 | **任务依赖图自动解锁** | tasks/ 模块已有 DAG，未接入 team | 1d |
| 3 | **Team Hooks** (TeammateIdle / TaskCompleted) | Hook 框架已有，缺 team HookPoint | 0.5d |

### P1：体验增强

| # | 改造项 | 当前状态 | 预估 |
|---|--------|---------|------|
| 4 | **用户直接与 teammate 交互** | 只能通过 lead | 3d |
| 5 | **长时 teammate 会话** (LONG_LIVED mode) | ephemeral one-shot | 2d |
| 6 | **Split-pane 显示** (tmux 集成) | 无 | 2d |

### P2：可选增强

| # | 改造项 | 当前状态 | 预估 |
|---|--------|---------|------|
| 7 | **团队配置持久化** (~/.claude/teams/) | 内存态 | 1d |
| 8 | **Session 恢复** (/resume 支持 teammates) | 无 | 3d |
| 9 | **多团队支持** (一个 lead 管多个 team) | 单 team | 1d |

---

## 5. 文件级改造清单（P0 项）

### 5.1 共享任务列表 + 自认领

**新增文件**：
- `agent_framework/team/task_board.py` — 共享任务面板

**修改文件**：
- `agent_framework/team/coordinator.py` — 添加 `create_task()` / `claim_task()` / `complete_task()`
- `agent_framework/tools/builtin/team_tools.py` — 添加 `team(action="create_task")` / `team(action="claim")` / `team(action="complete_task")`
- `agent_framework/agent/prompt_templates.py` — 补充任务列表使用说明

### 5.2 任务依赖图

**修改文件**：
- `agent_framework/team/task_board.py` — 任务依赖字段 + 自动解锁
- `agent_framework/tools/builtin/team_tools.py` — `team(action="create_task", depends_on=[...])`

### 5.3 Team Hooks

**修改文件**：
- `agent_framework/models/hook.py` — 新增 `TEAMMATE_IDLE` / `TEAM_TASK_COMPLETED`
- `agent_framework/team/coordinator.py` — finalize 后 fire hook
- `agent_framework/hooks/payloads.py` — 新增 payload 工厂函数

---

## 6. 结论

当前框架的 Team 实现在**通信协议**（4 种模式 + pub/sub）和**状态机**（10 态 + Q&A 续接 + 通知策略）方面已超越 Claude Code 的设计。主要差距在于：

1. **成员独立性**：当前是 sub-agent 模型（共享进程），Claude Code 是独立进程模型
2. **任务自协调**：当前是 lead 中心分配，Claude Code 支持 teammate 自主认领
3. **用户交互**：当前只能通过 lead，Claude Code 允许直接与任意 teammate 交互

这些差距不是"缺陷"，而是**架构选择**的不同：当前框架更适合"lead 编排"场景（适合 API/自动化），Claude Code 更适合"人工协作"场景（适合交互式开发）。两种模式可以共存。

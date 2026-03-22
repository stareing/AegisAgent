# Agent Team 对齐实施文档

> 基于 Claude Code Agent Teams 规范，将当前框架 Team 能力对齐至产品级标准。
> 保留当前框架的独有优势（4 种协作模式、10 态状态机、通知策略、Q&A 续接），补齐缺失能力。
> 规范来源：[agent_team_protocol_spec.md](agent_team_protocol_spec.md)
> 若本文档与协议规范冲突，以协议规范为准。

---

## 一、AT-* 合规矩阵现状

> 审查日期：2026-03-22
> 审查基准：`agent_team_protocol_spec.md` §13 Compliance Matrix

| ID | 要求 | 状态 | 通过条件 | 实现证据 |
|---|---|---|---|---|
| AT-001 | 真实 team config 持久化 | ✅ 通过 | config 持久化可查 | `TeamConfigStore` 磁盘读写 + `TeamConfigData` 模型 |
| AT-002 | 真实任务列表 | ✅ 通过 | task store 返回结构化任务 | `TeamTaskBoard` 完整实现 |
| AT-003 | 原子认领 | ✅ 通过 | 两个认领者不可同时成功 | `threading.Lock` + 10线程并发测试 |
| AT-004 | 依赖自动解锁 | ✅ 通过 | 依赖完成后 BLOCKED→PENDING | `_unblock_dependents_unlocked()` |
| AT-005 | 直接 teammate 消息 | ✅ 通过 | member_id 点对点送达 | `send_to_sibling()` teammate 间点对点；`/team-focus` 用户→teammate mail 路由 |
| AT-006 | 请求/回复关联 | ✅ 通过 | reply 携带 correlation_id | `mailbox.reply()` 设置 `correlation_id` |
| AT-007 | 计划审批阻止执行 | ✅ 通过 | 未审批不继续 | `_wait_for_approval()` + continuation |
| AT-008 | 长时 teammate 会话 | ✅ 通过 | 同 session_id 多 run_id | `TeamSessionManager` + coordinator 集成；session 跨 run 保持 session_id |
| AT-009 | 空闲通知 | ✅ 通过 | lead 收到结构化空闲通知 | `TEAMMATE_IDLE` 在默认升级类型 + hook 触发 |
| AT-010 | cleanup 拒绝脏状态 | ✅ 通过 | 活跃成员时 cleanup 失败 | `cleanup_team()` 检查 `BUSY_MEMBER_STATUSES` |
| AT-011 | cleanup 清理资源 | ✅ 通过 | 存储和句柄被清除 | 清除 task_board/answers/approvals/plans/shutdowns |
| AT-012 | hook 拦截完成 | ✅ 通过 | 拦截时状态不推进 | `complete_task()` 调 hook → DENY → 返回 `TEAM_HOOK_DENIED` |
| AT-013 | hook 通知空闲 | ✅ 通过 | hook 被触发 | `TEAMMATE_IDLE` 是 advisory hook（非 deniable），触发但不阻塞转换 |
| AT-014 | 用户直接聚焦 teammate | ✅ 通过 | 输入路由到 teammate 会话 | `coordinator.send_user_message_to_teammate()` 框架核心方法；`/team-focus` 路由 |
| AT-015 | progress 不作为完成 | ✅ 通过 | 无 progress→完成总结 | `PROGRESS_NOTICE` 不在默认升级类型中 |

**通过：15/15**

**AT 合规测试：`tests/test_team_at_compliance.py` — 32 项全通过**

---

## 二、实施路线（按 AT-* 覆盖优先级）

```
Phase 1: ✅ 已完成 — 共享任务面板 + 自认领 + 依赖图
         覆盖: AT-002 AT-003 AT-004
Phase 2: ✅ 已完成 — Team Hooks 定义
         覆盖: AT-012 AT-013 (HookPoint 已定义，DENIABLE 已声明)
Phase 3: 🔧 待实现 — Hook 运行时接入 + 错误模型 + Cleanup
         覆盖: AT-010 AT-011 AT-012 AT-013 AT-009
Phase 4: 🔧 待实现 — 长时 Teammate 会话 + 配置持久化
         覆盖: AT-001 AT-008
Phase 5: 🔧 待实现 — 用户直接交互 Teammate
         覆盖: AT-005 AT-014
```

---

## Phase 3: Hook 运行时接入 + 结构化错误 + Cleanup

### 3.1 目标 AT 条目

- AT-010: `cleanup_team()` 拒绝脏状态
- AT-011: cleanup 清理所有资源
- AT-012: `TEAMMATE_TASK_COMPLETED` hook 拦截阻止 complete_task
- AT-013: `TEAMMATE_IDLE` hook 拦截阻止 IDLE 转换
- AT-009: `TEAMMATE_IDLE` 通知自动升级到 lead

### 3.2 结构化错误模型

**新增** `agent_framework/models/team.py`:

```python
class TeamActionError(BaseModel, frozen=True):
    """Protocol-level structured error (spec §11)."""
    ok: bool = False
    error_code: str     # TEAM_MEMBER_BUSY, TEAM_TASK_BLOCKED, etc.
    message: str
    retryable: bool = False
```

标准错误码（spec §11）：
- `TEAM_NOT_INITIALIZED`
- `TEAM_MEMBER_NOT_FOUND` / `TEAM_MEMBER_BUSY`
- `TEAM_TASK_NOT_FOUND` / `TEAM_TASK_BLOCKED` / `TEAM_TASK_ALREADY_CLAIMED`
- `TEAM_REQUEST_NOT_FOUND`
- `TEAM_SPAWN_FAILED`
- `TEAM_CLEANUP_ACTIVE_MEMBERS`
- `TEAM_HOOK_DENIED`
- `TEAM_SESSION_NOT_FOUND`

### 3.3 Cleanup 实现

**修改** `team/coordinator.py`:

```python
def cleanup_team(self) -> dict:
    """Clean up team resources. Fails if active members exist (AT-010)."""
    from agent_framework.models.team import BUSY_MEMBER_STATUSES

    active = [
        m for m in self._registry.list_members()
        if m.role != "lead" and m.status in BUSY_MEMBER_STATUSES
    ]
    if active:
        return {
            "ok": False,
            "error_code": "TEAM_CLEANUP_ACTIVE_MEMBERS",
            "message": f"Cannot clean up while {len(active)} members are active",
            "active_members": [m.agent_id for m in active],
            "retryable": False,
        }

    # Clear all resources (AT-011)
    self._registry.clear()
    if self._task_board:
        self._task_board = None
    self._plans.clear()
    self._shutdowns.clear()
    self._pending_requests.clear()
    self._pending_answers.clear()
    self._pending_approvals.clear()
    self._active_teammate_ctx.clear()

    return {"ok": True, "team_id": self._team_id, "cleaned": True}
```

### 3.4 Hook 运行时接入

**修改** `team/coordinator.py` — `complete_task()`:

```python
def complete_task(self, task_id, result="", agent_id=""):
    # Before committing, fire TEAMMATE_TASK_COMPLETED hook
    if self._hook_executor is not None:
        from agent_framework.models.hook import HookPoint
        from agent_framework.hooks.payloads import teammate_task_completed_payload
        hook_result = self._hook_executor.fire_sync_advisory(
            HookPoint.TEAMMATE_TASK_COMPLETED,
            payload=teammate_task_completed_payload(...)
        )
        if hook_result and getattr(hook_result, "action", "") == "DENY":
            return {
                "ok": False,
                "error_code": "TEAM_HOOK_DENIED",
                "message": hook_result.feedback or "Completion blocked by hook",
                "retryable": True,
            }
    # Then commit
    task = self._task_board.complete_task(task_id, result, agent_id)
    ...
```

**修改** `team/coordinator.py` — `mark_result_delivered()`:

```python
def mark_result_delivered(self, agent_id):
    # Before IDLE, fire TEAMMATE_IDLE hook
    if self._hook_executor is not None:
        from agent_framework.models.hook import HookPoint
        hook_result = self._hook_executor.fire_sync_advisory(
            HookPoint.TEAMMATE_IDLE,
            payload=teammate_idle_payload(...)
        )
        if hook_result and getattr(hook_result, "action", "") == "DENY":
            return  # Don't transition to IDLE
    # Commit transition
    ...
```

### 3.5 TEAMMATE_IDLE 通知升级

**修改** `team/notification_policy.py`:

```python
_DEFAULT_ESCALATION_TYPES: frozenset[TeamNotificationType] = frozenset({
    TeamNotificationType.TASK_COMPLETED,
    TeamNotificationType.TASK_FAILED,
    TeamNotificationType.QUESTION,
    TeamNotificationType.ERROR,
    TeamNotificationType.TEAMMATE_IDLE,  # 新增
})
```

需要先在 `TeamNotificationType` 中添加 `TEAMMATE_IDLE`。

### 3.6 验收标准

1. `cleanup_team()` + 活跃成员 → `{"ok": false, "error_code": "TEAM_CLEANUP_ACTIVE_MEMBERS"}`
2. 所有成员 IDLE/SHUTDOWN 后 `cleanup_team()` → `{"ok": true, "cleaned": true}`
3. `complete_task()` + hook DENY → 任务状态不变，返回 `TEAM_HOOK_DENIED`
4. `mark_result_delivered()` + hook DENY → 成员保持 NOTIFYING
5. teammate 进入 IDLE → lead 收到 `TEAMMATE_IDLE` 通知

---

## Phase 4: 长时 Teammate 会话 + 配置持久化

### 4.1 目标 AT 条目

- AT-001: 团队配置持久化（磁盘可查）
- AT-008: 长时 teammate 会话（同 session_id 多 run_id）

### 4.2 TeamConfigStore

**新增** `agent_framework/team/config_store.py`:

```python
class TeamConfigStore:
    """Disk-backed team configuration store.

    Storage: ~/.agent/teams/{team_name}/config.json
    """

    def save(self, team_config: TeamConfigData) -> None: ...
    def load(self, team_name: str) -> TeamConfigData | None: ...
    def delete(self, team_name: str) -> None: ...
    def list_teams(self) -> list[str]: ...
```

### 4.3 TeamSessionState

**新增** `agent_framework/models/team.py`:

```python
class TeamSessionState(BaseModel):
    """Persistent state for a long-lived teammate session (spec §4.3)."""
    session_id: str
    team_id: str
    member_id: str
    status: TeamMemberStatus
    current_task_id: str = ""
    last_run_id: str = ""
    history_ref: str = ""
    created_at: datetime
    updated_at: datetime
```

### 4.4 LONG_LIVED 模式切换

**修改** `team/coordinator.py` — `_assign_task_async()`:

```python
spec = SubAgentSpec(
    parent_run_id=self._team_id,
    spawn_id=my_id,
    task_input=team_task,
    mode=SpawnMode.LONG_LIVED,  # 替换 EPHEMERAL
    ...
)
```

### 4.5 验收标准

1. `create_team()` → `~/.agent/teams/{name}/config.json` 生成
2. config 包含 `team_id`, `lead_id`, `members[]`, timestamps
3. 重启后 `load()` 恢复 team 配置
4. teammate 会话有持久 `session_id`，多次 run 用不同 `run_id`
5. `cleanup_team()` 删除磁盘配置

---

## Phase 5: 用户直接交互 Teammate

### 5.1 目标 AT 条目

- AT-005: 用户→teammate 直接消息送达
- AT-014: 用户可聚焦并输入到 teammate 会话

### 5.2 终端聚焦模型

**修改** `terminal_runtime.py`:

```python
class TeammateFocusState:
    focused_agent_id: str | None = None
    agents: list[str] = []

    def cycle_next(self) -> str | None:
        """Shift+Down: cycle to next teammate."""
        ...

    def is_focused(self) -> bool:
        return self.focused_agent_id is not None
```

**交互规则**：
- 用户输入在聚焦模式下 → `mail(send, to=focused_agent_id, payload={message: input})`
- 不经过 lead LLM
- teammate 的输出实时显示
- Escape → 回到 lead

### 5.3 Textual TUI

**修改** `textual_cli.py`:

```python
class TeammatePanel(Container):
    """Switchable panel showing a teammate's session."""
    agent_id: str
    # 显示 teammate 输出流
    # 接受直接输入
```

### 5.4 验收标准

1. REPL: Shift+Down → 显示 teammate 列表
2. 选中后输入直接到达 teammate
3. Escape → 回到 lead
4. teammate 输出实时显示
5. lead history 不被 teammate 交互污染

---

## 附录 A: Phase 与 AT-* 映射

| Phase | 目标 | AT 条目 | 状态 |
|---|---|---|---|
| 1 | 共享任务面板 + claim + 依赖图 | AT-002 AT-003 AT-004 | ✅ 已完成 |
| 2 | Team Hooks 定义 | AT-012 AT-013 (定义层) | ✅ 已完成 |
| 3 | Hook 接入 + 错误模型 + Cleanup | AT-009 AT-010 AT-011 AT-012 AT-013 | 🔧 待实现 |
| 4 | 长时会话 + 配置持久化 | AT-001 AT-008 | 🔧 待实现 |
| 5 | 用户直接交互 | AT-005 AT-014 | 🔧 待实现 |

## 附录 B: 验收记录模板

每个 Phase 提交时必须附带：

```md
## Phase X Acceptance Record

- Feature:
- Source of truth:
- Runtime path:
- AT-* coverage:
- Success tests:
- Failure tests:
- Example command:
- Example output:
- Known gaps:
```

## 附录 C: Phase 1 验收记录

```md
## Phase 1 Acceptance Record

- Feature: 共享任务面板 + 自认领 + 依赖图
- Source of truth: TeamTaskBoard (agent_framework/team/task_board.py)
- Runtime path: team(action="create_task") → coordinator.create_task() → task_board.create_task()
                team(action="claim") → coordinator.claim_task() → task_board.claim_task()
                team(action="complete_task") → coordinator.complete_task() → task_board.complete_task()
- AT-* coverage: AT-002 ✅, AT-003 ✅, AT-004 ✅
- Success tests: tests/test_team_task_board.py (27 passed)
  - TestCreateTask (5 tests): CRUD + dependency auto-block
  - TestClaimTask (6 tests): specific/auto claim, blocked rejection, double claim
  - TestConcurrentClaim (1 test): 5 threads, only 1 wins
  - TestCompleteTask (4 tests): mark complete, auto-unblock, multi-dep
  - TestFailTask (1 test): mark failed
  - TestListAndFilter (5 tests): list all, filter by status/assignee, claimable, count
  - TestCoordinatorTaskBoard (5 tests): coordinator methods, lifecycle, status integration
- Failure tests:
  - claim_blocked_task_returns_none: BLOCKED task cannot be claimed
  - double_claim_fails: second claimer gets None
  - complete_already_completed_returns_none: terminal state guard
  - claim_without_board_returns_error: uninitialized board error
- Example command: team(action="create_task", task="Fix parser bug")
- Example output: {"created": true, "task_id": "task_abc123", "title": "Fix parser bug", "status": "pending"}
- Known gaps: AT-001 (config not persisted), error model not structured per spec §11
```

## 附录 D: Phase 2 验收记录

```md
## Phase 2 Acceptance Record

- Feature: Team Hooks 定义
- Source of truth: HookPoint enum (agent_framework/models/hook.py)
- Runtime path: HookPoint.TEAMMATE_TASK_COMPLETED → DENIABLE
                HookPoint.TEAMMATE_IDLE → advisory
                teammate_task_completed_payload() / teammate_idle_payload()
- AT-* coverage: AT-012 (定义层 ✅, 运行时 ❌), AT-013 (定义层 ✅, 运行时 ❌)
- Success tests: tests/test_team_hooks.py (8 passed)
  - TestHookPointEnum (4 tests): exists, deniable
  - TestPayloadFactories (3 tests): structure, truncation
  - TestCoordinatorHookExecutor (1 test): attribute exists
- Failure tests:
  - test_teammate_idle_is_not_deniable: IDLE is advisory, not deniable
  - test_result_summary_truncated: payload truncation at 500 chars
- Example: HookPoint.TEAMMATE_TASK_COMPLETED in DENIABLE_HOOK_POINTS → True
- Known gaps: hook 未接入 complete_task() 和 mark_result_delivered() 运行时路径
```

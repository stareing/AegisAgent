# Task System & Background Execution — Development Guide

> 三个递进层次：进程内 Todo (s03) → 持久化任务图 (s07) → 后台并发执行 (s08)

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────┐
│  RunCoordinator (iteration loop)                        │
│                                                         │
│  每轮前:                                                │
│    1. drain background notifications → inject messages  │
│    2. inject <todo-state> into runtime_info              │
│                                                         │
│  每轮后:                                                │
│    3. register new background task_ids                  │
│    4. track rounds_since_task_write                     │
│                                                         │
│  ┌──────────────┐  ┌───────────────┐  ┌──────────────┐ │
│  │ TaskManager   │  │ BashSession   │  │ Background   │ │
│  │ (磁盘 DAG)   │  │ (独立进程)    │  │ Notifier     │ │
│  └──────────────┘  └───────────────┘  └──────────────┘ │
│        ↑                   ↑                  ↑         │
│  ToolExecutor._route_local() 拦截 task_*/bash_* 工具    │
└─────────────────────────────────────────────────────────┘
         ↓                   ↓                  ↓
    .tasks/              独立 subprocess     coordinator
    task_N.json          (不经 _lock)        实例级存活
```

---

## 2. s03 — TodoManager (Reminder Injection)

### 核心设计

模型通过 `task_create`/`task_update` 自主管理进度。当连续 3 轮未使用任务工具时，
框架在 system prompt 中注入 `<reminder>` 提醒模型更新任务状态。

```
+----------+      +-------+      +---------+
|   User   | ---> |  LLM  | ---> | Tools   |
|  prompt  |      |       |      | + task  |
+----------+      +---+---+      +----+----+
                      ^               |
                      |   tool_result |
                      +---------------+
                            |
                +-----------+-----------+
                | TaskManager state     |
                | [ ] task 1 - pending  |
                | [>] task 2 - active   |
                | [x] task 3 - done     |
                +-----------------------+
                            |
                if rounds_since_task >= 3:
                  inject <todo-state><reminder>
```

### 实现位置

| 组件 | 文件 | 职责 |
|---|---|---|
| `TaskManager` | `tools/todo.py` | CRUD + 依赖图 + 磁盘持久化 |
| `TaskService` | `tools/todo.py` | run_id → TaskManager 映射 |
| `_track_todo_round()` | `agent/coordinator.py` | 每轮检测 task_create/task_update 调用 |
| `_collect_runtime_info()` | `agent/coordinator.py` | 注入 todo_summary + todo_reminder |
| `collect_system_core()` | `context/source_provider.py` | 渲染 `<todo-state>` XML 块 |

### Reminder 触发条件

```python
# tools/todo.py
def should_remind(self) -> bool:
    return self.has_tasks and self._rounds_since_write >= 3
```

三个条件同时满足才触发：
1. 存在至少一个任务（`has_tasks`）
2. 连续 3 轮未调用 `task_create` 或 `task_update`
3. Reminder 注入到 `<todo-state>` XML 块，**不修改用户消息**

### 注入格式

```xml
<todo-state>
  <summary>2/5 done, 1 active, 1 ready, 1 blocked</summary>
  <reminder>The task list hasn't been updated recently. Consider using
  task_update to mark progress.</reminder>
</todo-state>
```

### 对比 s03 示例

| s03 示例 | 框架实现 | 差异 |
|---|---|---|
| 单个 `todo` 工具，bulk update | 4 个独立工具 `task_create/update/list/get` | 框架更清晰 |
| 内存存储 | 磁盘 JSON 文件 (`.tasks/`) | 框架持久化 |
| `items: [{id, text, status}]` | `subject, description, status, metadata, ...` | 框架字段更丰富 |
| 只有 3 种状态 | 4 种：pending/in_progress/completed/**deleted** | 框架多 deleted |
| 无依赖关系 | `blockedBy`/`blocks` DAG | 框架有依赖图 |
| reminder 插入 user message | reminder 走 runtime_info → system_core | 框架不篡改会话 |

---

## 3. s07 — 持久化任务图 (Task DAG)

### 核心设计

每个任务是 `.tasks/task_N.json` 文件，有状态、前置依赖 (`blockedBy`) 和后置依赖 (`blocks`)。
完成任务时自动从下游的 `blockedBy` 中移除，解锁后续任务。

```
.tasks/
  task_1.json  {"id":1, "status":"completed"}
  task_2.json  {"id":2, "blockedBy":[1], "status":"pending"}
  task_3.json  {"id":3, "blockedBy":[1], "status":"pending"}
  task_4.json  {"id":4, "blockedBy":[2,3], "status":"pending"}

        +----------+
   +--> | task 2   | --+
   |    | pending  |   |
+--+--+ +----------+   +--> +----------+
|  1  |                     | task 4   |
|done | --> +----------+  > | blocked  |
+-----+    | task 3   | -+ +----------+
           | pending  |
           +----------+
```

### 四个工具 API

```
task_create(subject, description?, blocked_by?, active_form?, metadata?)
  → {"id": 1, "subject": "...", "status": "pending", "blockedBy": [], ...}

task_update(task_id, status?, subject?, active_form?, add_blocked_by?, add_blocks?, owner?, metadata?)
  → {"id": 1, "status": "completed", ...}
  → status="deleted" 时删除文件并清理所有依赖边

task_list()
  → {"tasks": [...], "summary": {total, ready, blocked, in_progress, completed},
     "ready_task_ids": [2,3], "blocked_task_ids": [4]}

task_get(task_id)
  → {"id": 1, "subject": "...", ...}
```

### 依赖解除

```python
# 完成 task 1 时自动执行
def _clear_dependency(self, completed_id: int):
    for task_file in self.dir.glob("task_*.json"):
        task = load(task_file)
        if completed_id in task["blockedBy"]:
            task["blockedBy"].remove(completed_id)
            save(task)
```

### 删除清理

```python
# status="deleted" 时执行
def _delete_task(self, task_id, task):
    # 1. 从所有其他任务的 blockedBy/blocks 中移除此 ID
    # 2. 删除 .tasks/task_N.json 文件
```

### 任务结构 (JSON)

```json
{
  "id": 2,
  "subject": "Implement auth middleware",
  "description": "JWT validation + session management",
  "status": "pending",
  "blockedBy": [1],
  "blocks": [3, 4],
  "owner": "agent-1",
  "activeForm": "Implementing auth middleware",
  "metadata": {"priority": "p0", "sprint": "2026-Q1"},
  "created_at": 1710700000.0,
  "updated_at": 1710700000.0
}
```

### Run-scoped 路由

```python
# tools/executor.py — ToolExecutor._route_local()
if entry.meta.name in _TASK_TOOLS and self._current_run_id:
    mgr = self._todo_service.get(self._current_run_id)
    if name == "task_create":
        return mgr.create(...)
    elif name == "task_update":
        return mgr.update(...)
    ...
```

所有 run 共享同一个项目级 `.tasks/` 目录，任务跨 run 持久化。

### 对比 s07 示例

| s07 示例 | 框架实现 | 差异 |
|---|---|---|
| `TaskManager` 类 | `TaskManager` 类 | 结构一致 |
| `create(subject, description)` | `create(subject, description, blocked_by, active_form, metadata)` | 框架更多字段 |
| `update(task_id, status, addBlockedBy, addBlocks)` | `update(task_id, status, ..., metadata, active_form, owner)` | 框架增加 metadata/owner/deleted |
| 无 deleted | `status="deleted"` → 删文件 + 清边 | 框架有 |
| camelCase (`addBlockedBy`) | snake_case (`add_blocked_by`) | 命名规范差异 |
| 全局单例 `TASKS` | `TaskService` run-scoped 注册 | 框架隔离性更好 |
| `list_all()` 返回文本 | `list_all()` 返回 JSON + ready/blocked 分类 | 框架更结构化 |

---

## 4. s08 — 后台并发执行

### 核心设计

慢命令（`npm install`、`pytest`、`docker build`）丢后台独立 subprocess，
agent 继续做其他事。完成后在下一轮 LLM 调用前自动注入 `<background-results>`。

```
Main thread                 Independent subprocess
+-----------------+         +-----------------+
| agent loop      |         | command runs    |
| [other work]    |         | (no lock held)  |
| ...             |         | ...             |
| [next LLM] <---+---------| result ready    |
|  ^drain queue   |         +-----------------+
+-----------------+

Timeline:
Agent --[bash_exec bg=True]--[bash_exec bg=True]--[edit_file]--[LLM call]--
              |                      |                             ^
              v                      v                             |
           [A runs independently] [B runs independently]    drain results
              |                      |                      inject messages
              +--- both complete --> notification queue ---->+
```

### 关键改进：独立 subprocess

s08 示例用 `threading.Thread` + `subprocess.run`。框架用 `asyncio.create_subprocess_shell`，
**不经过 BashSession 的 `_lock`**，多个后台任务真正并行执行。

```python
# shell_manager.py — execute_background()
async def _run_independent():
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=build_safe_env(),
        start_new_session=True,  # 独立进程组
    )
    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    ...
```

### 三个工具

```
bash_exec(command, timeout_seconds=120, run_in_background=False)
  → 前台: {"output": "...", "exit_code": 0}
  → 后台: {"task_id": "abc123", "status": "running"}

bash_output(task_id, block=False, timeout_ms=30000)
  → 完成: {"output": "...", "exit_code": 0}
  → 运行中: {"status": "running", "task_id": "abc123"}
  → block=True 时阻塞等待直到完成或超时

bash_stop(task_id)
  → {"output": "Background task abc123 stopped", "cancelled": true}
  → 发送 SIGKILL 杀掉 OS 进程组
```

### 自动通知注入

```python
# coordinator.py — 每轮 LLM 调用前
def _drain_background_notifications(self, session_state):
    notifications = self._bg_notifier.drain()
    if notifications:
        text = BackgroundNotifier.format_notifications(notifications)
        # 注入 user + assistant 消息对
        self._state_ctrl.append_user_message(session_state,
            Message(role="user", content=text))
        self._state_ctrl.append_projected_messages(session_state,
            [Message(role="assistant", content="Noted background results.")])
```

注入格式：
```xml
<background-results>
[bg:abc123] (success) npm install completed...
[bg:def456] (exit=1) Error: test_auth failed
</background-results>
```

### 生命周期

| 阶段 | 动作 |
|---|---|
| `bash_exec(run_in_background=True)` | 创建独立 subprocess，返回 task_id |
| `_register_background_tasks()` | 检测 `{task_id, status: running}`，注册到 notifier |
| `_drain_background_notifications()` | 每轮前轮询完成的任务，注入消息 |
| `bash_stop(task_id)` | 单任务停止：`task.cancel()` + SIGKILL |
| `kill_shell()` | 停所有后台任务 + 杀持久 shell |
| run 结束 | **不清空 notifier** — 跨 run 持续追踪 |

### 取消链 (CancelledError → SIGKILL)

```python
# shell_manager.py — _run_independent()
except asyncio.CancelledError:
    _kill_proc(proc)  # SIGKILL 进程组
    result = {"cancelled": True, ...}
    self._background_results[task_id] = result
    raise  # re-raise for asyncio
```

### 对比 s08 示例

| s08 示例 | 框架实现 | 差异 |
|---|---|---|
| `threading.Thread` | `asyncio.create_subprocess_shell` | 框架更现代 |
| `background_run` + `check_background` | `bash_exec(bg=True)` + `bash_output` + `bash_stop` | 框架多单任务 stop |
| 共享 subprocess.run | 独立进程 + `start_new_session=True` | 框架真正并行 |
| 全局 `BackgroundManager` | coordinator 实例级 `BackgroundNotifier` | 框架跨 run 存活 |
| drain 在 agent_loop 顶部 | drain 在 `_prepare_llm_request` 前 | 位置一致 |
| 无取消机制 | `bash_stop` + SIGKILL 进程组 | 框架有 |
| `check_background` 只能查状态 | `bash_output(block=True, timeout_ms=)` 可阻塞等待 | 框架更灵活 |

---

## 5. 工具对齐表 (Claude Code ↔ 框架)

| Claude Code 原生 | 框架工具 | 对齐状态 |
|---|---|---|
| `TaskCreate(subject, description, activeForm, metadata)` | `task_create(subject, description, active_form, metadata, blocked_by)` | 对齐 + 依赖图 |
| `TaskUpdate(status incl. deleted, metadata null-delete)` | `task_update(status incl. deleted, metadata null-delete)` | 对齐 |
| `TaskList` | `task_list` → + ready/blocked/in_progress 分类 | 对齐 + 增强 |
| `TaskGet` | `task_get` | 对齐 |
| `TaskOutput(block, timeout)` | `bash_output(block, timeout_ms)` | 对齐 |
| `TaskStop(task_id)` | `bash_stop(task_id)` | 对齐 |

---

## 6. 测试覆盖

```bash
# 任务系统测试 (49 tests)
pytest tests/test_todo.py -v

# 后台任务测试 (27 tests)
pytest tests/test_background.py -v

# 关键测试类
TestTaskCRUD              # CRUD + activeForm + metadata + deleted
TestDependencyGraph       # blockedBy/blocks + 自动解锁 + 部分解锁
TestRunIsolation          # 跨 run 共享 + 隔离
TestReminder              # 3 轮阈值 + 重置 + 空任务不触发
TestDiskPersistence       # 重建 manager 后数据存活
TestParallelExecution     # 两个 sleep 真正并行 (<2.5s)
TestBashStopSingleTask    # 单任务停止 + 不影响其他任务
TestKillShellBackgroundOnly  # OS pid 存活性验证
TestCrossRunPersistence   # notifier 跨 run 不重建
```

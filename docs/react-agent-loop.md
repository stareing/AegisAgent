# ReAct Agent Loop — 框架实现详解

> 基于 API 原生 tool_use 协议的 ReAct 循环，非 XML 文本解析

---

## 1. 与 XML 解析式 ReAct 的根本区别

```
XML 解析式（旧）                      API 原生 tool_use（本框架）
─────────────────                      ────────────────────────────
模型输出纯文本                          模型输出结构化 tool_calls
  ↓                                       ↓
正则解析 <action>...</action>           response.tool_calls: list[ToolCallRequest]
  ↓                                       ↓
手动拆分 tool_name, args               block.function_name + block.arguments (JSON)
  ↓                                       ↓
<observation> 包装结果                   tool_result 消息类型 (API 标准)
  ↓                                       ↓
每轮只能一个工具                         每轮可并行多工具 (batch_execute)
```

**本框架不做任何 XML/正则解析。** 工具调用和结果都走 API 的结构化协议。

---

## 2. 两层分离架构

```
RunCoordinator (编排层)               AgentLoop (执行层)
┌─────────────────────────┐          ┌──────────────────────┐
│ while True:             │          │ execute_iteration():  │
│   ① 超时/取消检查       │          │   1. call LLM        │
│   ② drain 后台通知      │          │   2. check stop      │
│   ③ prepare_llm_request │──────→   │   3. dispatch tools   │
│   ④ execute_iteration   │   ←────  │   4. return Result    │
│   ⑤ apply result        │          └──────────────────────┘
│   ⑥ track todo round    │
│   ⑦ register bg tasks   │          AgentLoop 零写入：
│   ⑧ check should_stop   │          - 不改 AgentState
│   ⑨ break or continue   │          - 不改 SessionState
└─────────────────────────┘          - 只返回 IterationResult
```

**RunCoordinator** 决定 WHEN，**AgentLoop** 决定 HOW。

---

## 3. 完整循环流程（RunCoordinator.run）

```python
async def run(self, agent, deps, task, ...) -> AgentRunResult:
    # ── 初始化 ────────────────────────────────────────────
    agent_state = AgentState(run_id=run_id, task=task)
    session_state = SessionState(session_id=..., run_id=run_id)

    # 写入用户消息 (sole write-port)
    self._state_ctrl.append_user_message(session_state, user_msg)

    # 激活 Skill / 应用 Policy / 初始化 Memory Session
    deps.memory_manager.begin_run_session(run_id, agent_id, user_id)
    effective_config = self._policy_resolver.resolve(agent, skill)

    # ── 主循环 ────────────────────────────────────────────
    while True:
        # ①  Guard: 超时
        if elapsed_ms >= timeout_ms:
            break  # → StopReason.MAX_ITERATIONS

        # ②  Guard: 外部取消
        if cancel_event and cancel_event.is_set():
            break  # → StopReason.USER_CANCEL

        # ③  s08: drain 后台任务完成通知 → 注入 <background-results>
        self._drain_background_notifications(session_state)

        # ④  构建 LLM 请求（上下文工程）
        #    runtime_info 中包含:
        #    - 环境 (os, cwd)
        #    - 能力 (spawn, parallel, iterations)
        #    - 任务状态 (todo_summary, todo_reminder)  ← s03
        llm_request = await self._prepare_llm_request(
            agent, deps, agent_state,
            session_state=session_state,
            effective_config=effective_config,
            active_skill=active_skill,
            task=task,
        )

        # ⑤  执行单次迭代（AgentLoop — 零写入）
        iteration_result = await self._loop.execute_iteration(
            agent, loop_deps,
            agent_state, llm_request, effective_config,
        )

        # ⑥  应用结果（唯一写口）
        self._state_ctrl.apply_iteration_result(agent_state, iteration_result)
        self._state_ctrl.project_iteration_to_session(session_state, iteration_result)

        # ⑦  s03: 追踪 task 工具调用轮数
        self._track_todo_round(deps, iteration_result)

        # ⑧  s08: 注册新的后台任务 task_id
        self._register_background_tasks(iteration_result)

        # ⑨  检查停止条件（返回 StopDecision，不是 bool）
        stop_decision = agent.should_stop(iteration_result, agent_state)
        if stop_decision.should_stop:
            break

    # ── 收尾 ──────────────────────────────────────────────
    self._state_ctrl.mark_finished(agent_state)
    deps.memory_manager.record_turn(task, final_answer, iteration_history)
    return AgentRunResult(...)
```

---

## 4. 单次迭代内部（AgentLoop.execute_iteration）

```python
async def execute_iteration(self, agent, loop_deps, agent_state,
                             llm_request, effective_config) -> IterationResult:
    idx = agent_state.iteration_count

    # 1. 调用 LLM（API 原生 tool_use）
    model_response = await self._call_llm(
        loop_deps.model_adapter,
        llm_request.messages,
        llm_request.tools_schema,  # JSON Schema 格式的工具定义
        effective_config,
    )
    # model_response.tool_calls: list[ToolCallRequest] | []
    # model_response.finish_reason: "stop" | "tool_calls" | "length"

    # 2. 检查停止条件
    stop_signal = self._check_stop_conditions(agent, model_response, agent_state)
    if stop_signal:
        return IterationResult(stop_signal=stop_signal, ...)

    # 3. 分发工具调用（可并行）
    if model_response.tool_calls:
        tool_results, tool_metas = await self._dispatch_tool_calls(
            agent, loop_deps.tool_executor,
            model_response.tool_calls,  # 结构化: [{function_name, id, arguments}, ...]
            agent_state,
        )

    # 4. 返回不可变结果（不写任何状态）
    return IterationResult(
        iteration_index=idx,
        model_response=model_response,
        tool_results=tool_results,
        tool_execution_meta=tool_metas,
    )
```

---

## 5. 工具调用协议对比

### API 原生 tool_use（本框架使用）

```
LLM 请求:
  messages: [{role: "system", ...}, {role: "user", ...}]
  tools: [{name: "read_file", parameters: {...}}, ...]

LLM 响应:
  finish_reason: "tool_calls"
  tool_calls: [
    {id: "tc_1", function_name: "read_file", arguments: {"path": "README.md"}},
    {id: "tc_2", function_name: "list_directory", arguments: {"path": "tests"}},
  ]

工具执行（并行）:
  results = await executor.batch_execute(tool_calls)
  → [(ToolResult, ToolExecutionMeta), ...]

投影到会话:
  messages.append({role: "assistant", tool_calls: [...]})
  messages.append({role: "tool", tool_call_id: "tc_1", content: "..."})
  messages.append({role: "tool", tool_call_id: "tc_2", content: "..."})
```

### XML 解析式（不应使用）

```
LLM 请求:
  messages: [{role: "user", content: "...请用 <action> 标签..."}]

LLM 响应:
  content: "<thought>我需要读文件</thought>\n<action>read_file(README.md)</action>"

手动解析:
  re.search(r"<action>(.*?)</action>", content)  # 脆弱
  tool_name, args = parse_action(match.group(1))   # 手动拆分

返回:
  messages.append({role: "user", content: "<observation>...</observation>"})
```

---

## 6. 六层终止条件

```
优先级   条件                      StopReason            TerminationKind
─────   ──────────────────         ──────────────        ───────────────
  1     LLM 返回 finish=stop      LLM_STOP              NORMAL
  2     达到 max_iterations        MAX_ITERATIONS         DEGRADE
  3     输出被截断                 OUTPUT_TRUNCATED       DEGRADE
  4     连续 3 次 LLM 错误        ERROR                  ABORT
  5     外部 cancel_event          USER_CANCEL            ABORT
  6     wall-clock 超时            MAX_ITERATIONS         DEGRADE

附加守卫:
  - 重复工具调用检测: 同一工具+同参数连续调用 2 次 → 强制停止
  - OrchestratorAgent: spawn 后 N 轮无综合 → 强制退出
```

---

## 7. s03/s07/s08 在循环中的集成点

```
while True:
    ┌─────────────────────────────────────────────────┐
    │  ③ drain_background_notifications()        [s08] │
    │     后台任务完成 → 注入 <background-results>     │
    │     到 SessionState（user + assistant 消息对）    │
    ├─────────────────────────────────────────────────┤
    │  ④ prepare_llm_request()                         │
    │     → _collect_runtime_info()                    │
    │       → todo_summary: "2/5 done, 1 active" [s03] │
    │       → todo_reminder: "Update tasks"      [s03] │
    │     → ContextSourceProvider.collect_system_core() │
    │       → <todo-state> XML block             [s03] │
    ├─────────────────────────────────────────────────┤
    │  ⑤ execute_iteration()                           │
    │     → LLM sees task tools in schema              │
    │     → LLM may call task_create/task_update  [s07] │
    │     → LLM may call bash_exec(bg=True)       [s08] │
    ├─────────────────────────────────────────────────┤
    │  ⑦ _track_todo_round()                     [s03] │
    │     本轮调了 task_create/update? → reset counter  │
    │     没调? → rounds_since_write += 1              │
    ├─────────────────────────────────────────────────┤
    │  ⑧ _register_background_tasks()            [s08] │
    │     检测 bash_exec 返回 {task_id, status:running} │
    │     → notifier.register(task_id)                 │
    └─────────────────────────────────────────────────┘
```

---

## 8. 数据流总览

```
用户输入
  ↓
RunCoordinator.run()
  ├→ _build_user_message(task)           → Message(role="user")
  ├→ _state_ctrl.append_user_message()   → SessionState.messages
  │
  ├→ while True:
  │   ├→ _drain_background_notifications()
  │   │     BackgroundNotifier.drain()
  │   │     → Message(role="user", content="<background-results>...")
  │   │     → Message(role="assistant", content="Noted...")
  │   │
  │   ├→ _prepare_llm_request()
  │   │     ├→ memory_manager.select_for_context()
  │   │     ├→ _collect_runtime_info()  ← todo_summary, todo_reminder
  │   │     ├→ context_engineer.prepare_context_for_llm()
  │   │     │     ├→ source_provider.collect_system_core()
  │   │     │     │     → <system-identity>
  │   │     │     │     → <runtime-environment>
  │   │     │     │     → <agent-capabilities>
  │   │     │     │     → <todo-state>           ← s03 注入点
  │   │     │     │     → <available-tools>
  │   │     │     ├→ source_provider.collect_skill_addon()
  │   │     │     ├→ source_provider.collect_saved_memory_block()
  │   │     │     └→ session history + current input
  │   │     └→ LLMRequest(messages, tools_schema)
  │   │
  │   ├→ AgentLoop.execute_iteration()
  │   │     ├→ adapter.complete(messages, tools)  ← API 原生 tool_use
  │   │     ├→ check_stop_conditions()
  │   │     ├→ dispatch_tool_calls()              ← batch_execute 并行
  │   │     └→ IterationResult (不可变)
  │   │
  │   ├→ _state_ctrl.apply_iteration_result()     ← 唯一写口
  │   ├→ _state_ctrl.project_iteration_to_session()
  │   ├→ _track_todo_round()                      ← s03
  │   ├→ _register_background_tasks()             ← s08
  │   └→ agent.should_stop() → StopDecision
  │
  └→ AgentRunResult(final_answer, stop_signal, usage, ...)
```

---

## 9. 工具执行路由

```python
# ToolExecutor._route_local() — 拦截 + 路由

if entry.meta.name in _TASK_TOOLS:     # task_create/update/list/get
    mgr = self._todo_service.get(run_id)  # run-scoped TaskManager
    return mgr.create/update/list/get()

if entry.meta.name == "bash_exec":      # 普通执行或后台
    if args.get("run_in_background"):
        return session.execute_background()  # 独立 subprocess
    return session.execute()                 # 持久 session

# 其他本地工具: 直接调用 callable_ref
return await entry.callable_ref(**args)
```

---

## 10. 关键不变量

| 不变量 | 实现 |
|---|---|
| AgentLoop 零写入 | 只返回 IterationResult，不改 AgentState/SessionState |
| 唯一写口 | 只有 RunStateController 修改运行时状态 |
| 工具结果顺序稳定 | `asyncio.gather` 保证按输入顺序返回 |
| 后台任务不阻塞主循环 | 独立 `create_subprocess_shell`，不经 BashSession._lock |
| Reminder 不改 TodoState | 只注入 XML 块到 system_core，不写任务数据 |
| 后台通知跨 run 存活 | BackgroundNotifier 是 coordinator 实例级，run 结束不清空 |
| 终止必分类 | 每个 StopSignal 派生 TerminationKind: NORMAL/ABORT/DEGRADE |

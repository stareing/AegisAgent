# Context KV Cache 架构设计

## 最终消息序列

```
迭代 0:
  [0] system (frozen prefix)              ← IMMUTABLE
  [1] user("task text")                   ← from SessionState
  [2] user("<context-update>              ← 仅迭代 0 有
        <session-context>date,platform,git</session-context>
        <saved-memories>...</saved-memories>
      </context-update>")

迭代 1:
  [0] system (frozen prefix)              ← KV HIT ✅
  [1] user("task text")                   ← KV HIT ✅
  [2] user("<context-update>...")          ← KV HIT ✅ (内容未变)
  [3] assistant(tool_calls=[...])         ← new
  [4] tool(result)                        ← new

迭代 2:
  [0] system (frozen prefix)              ← KV HIT ✅
  [1] user("task text")                   ← KV HIT ✅
  [2] user("<context-update>...")          ← KV HIT ✅
  [3] assistant(tool_calls=[...])         ← KV HIT ✅
  [4] tool(result)                        ← KV HIT ✅
  [5] assistant(tool_calls=[...])         ← new
  [6] tool(result)                        ← new

迭代 N (无 memory 变化):
  [0..2N] 全部 KV HIT                    ← 缓存复用率 → 100%
  [2N+1..2N+2] new                        ← 仅最新一轮
```

## KV Cache 正确性验证

| 条件 | injection 是否变化 | 缓存影响 |
|---|---|---|
| **迭代间无 memory 变化** | `<context-update>` 内容完全相同 | **0 失效** — 整个前缀+历史全部命中 |
| **memory 被提取/修改** | `<saved-memories>` 内容变化 | injection 位置在最末 → **只失效 injection 本身** |
| **首迭代 → 后续迭代** | session_context 只在首迭代注入 | 后续迭代 injection 少了 session_context → 内容变化 → **injection 失效，前面不受影响** |
| **无 memory + 非首迭代** | injection_parts 为空 → **不追加任何消息** | 纯 `[system][history...]` 序列，**KV cache 100% 复用** |

## Frozen Prefix 轮换触发条件（穷举）

- ✅ `system_prompt` 文本变化
- ✅ tools 增删（`tool_entries` 变化，含 MCP sync）
- ✅ skill 激活/停用（`skill_addon` 变化）
- ✅ `approval_mode` 切换（进入 `system_core`）
- ❌ 迭代进度 — 不轮换
- ❌ 记忆变化 — 不轮换
- ❌ todo 状态 — 不轮换（已移除 dynamic_state）
- ❌ 压缩发生 — 不轮换

## 唯一已知的 KV Cache 失效场景

| 场景 | 影响 | 频率 |
|---|---|---|
| tools/MCP 动态注册/移除 | 全部缓存失效 | 极罕见（run 内几乎不发生） |
| skill 激活/停用 | 全部缓存失效 | 每 run 最多 1 次 |
| 压缩触发（旧 groups 被摘要替代） | 被压缩的 groups 缓存失效 | 长对话时偶尔触发 |
| approval_mode 切换 | 全部缓存失效 | 手动切换，极罕见 |

## 数据流全链路（每次迭代）

```
RunCoordinator._prepare_llm_request()
  │
  ├─① 收集 runtime_info = _collect_runtime_info()
  │     静态: os, cwd, max_iterations, can_spawn, parallel_tool_calls, approval_mode
  │     排除: current_iteration, spawned_subagents, todo_summary, todo_reminder
  │
  ├─② 组装 context_materials = {
  │     agent_config, session_state(snapshot), memories,
  │     task, active_skill, runtime_info, skill_descriptions, tool_entries
  │   }
  │
  └─③ ContextEngineer.prepare_context_for_llm(agent_state, context_materials)
        │
        ├─ collect_system_core(config, runtime_info, tool_entries)
        │    → 只取静态 key → system_core (str)
        │
        ├─ collect_skill_addon(active_skill) + skill_catalog
        │    → skill_addon (str | None)
        │
        ├─ PrefixManager.get_or_create(system_core, skill_addon)
        │    hash = SHA256(system_core + skill_addon)[:16]
        │    if hash == cached → 复用 (prefix_reused=True)
        │    else → 重建 Message(role="system", content=...)
        │    → prefix.messages = [frozen_system_msg]
        │
        ├─ collect_session_context(runtime_info) [仅 iter==0]
        │    → "<session-context>date, platform, git...</session-context>"
        │
        ├─ collect_saved_memory_block(memories)
        │    → "<saved-memories>...</saved-memories>"
        │
        ├─ collect_session_groups(session_state)
        │    → [Group(user), Group(asst+tools, tool_results), ...]
        │
        ├─ compress_groups_async(groups, budget) [if stateless + allowed]
        │    → 裁剪最旧的 groups → 保留 recent detail
        │
        └─ 组装最终 messages:
             [0]     frozen_prefix           ← IMMUTABLE
             [1..N]  session history          ← STABLE (append-only)
             [N+1]   user("<context-update>   ← LAST, 仅在有内容时追加
                       <session-context>...
                       <saved-memories>...
                     </context-update>")
```

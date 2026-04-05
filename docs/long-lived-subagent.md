# LONG_LIVED 子 Agent 开发文档

## 一、问题定义

当前所有 `spawn_agent` 调用（包括 `mode=LONG_LIVED`）的实际行为都等同于 EPHEMERAL：
- 每次 spawn 创建全新 agent + deps + SessionState
- coordinator.run() 结束后全部销毁
- 下次 spawn 对之前对话一无所知
- SHARED_WRITE 只共享长期记忆，不共享对话历史

**结果**：无法与单个子 agent 进行多轮持续对话。

## 二、目标

实现 Claude Code 式的子 agent 交互模式：

```
spawn_agent(mode="LONG_LIVED", task_input="分析代码")
  → Agent 执行 → 返回结果 → Agent 进入 IDLE（不销毁）

send_message(spawn_id="abc", message="上次分析了什么？")
  → Agent 恢复 → 在完整历史上下文中继续 → 返回结果 → 再次 IDLE

send_message(spawn_id="abc", message="基于分析修复 bug")
  → Agent 继续累积上下文 → 执行 → 返回
```

## 三、设计

### 3.1 生命周期状态

```
spawn(EPHEMERAL) → RUNNING → COMPLETED → 销毁
spawn(LONG_LIVED) → RUNNING → IDLE（存活等待）
                                 ↓
                    send_message → RUNNING → IDLE
                                              ↓
                    close_agent / 父run结束 / TTL → 销毁
```

新增状态：`IDLE` — 任务完成但 agent 存活，等待下一条消息。

### 3.2 存储结构

```python
class _LiveAgent:
    """LONG_LIVED agent 的存活上下文。"""
    agent: BaseAgent
    deps: AgentRuntimeDeps
    session_state: SessionState
    handle: SubAgentHandle
    last_active: float  # time.monotonic()
```

```python
class SubAgentRuntime:
    _active: dict[str, SubAgentHandle]     # 运行中的 agent（所有模式）
    _live_agents: dict[str, _LiveAgent]    # LONG_LIVED 存活池
```

### 3.3 三条销毁路径

| 触发 | 时机 | 行为 |
|------|------|------|
| 父 run 结束 | coordinator.run() 返回时 | 清理该 run 下所有 IDLE agent |
| 显式关闭 | LLM 调用 `close_agent(spawn_id)` | 立即销毁指定 agent |
| TTL 超时 | drain 时检查 | 超过 `live_agent_ttl_seconds` 未活动 → 自动清理 |

### 3.4 工具 API

```
spawn_agent(task_input, mode="LONG_LIVED", ...)
  → 首次启动，返回 {"spawn_id": "abc", "status": "IDLE", ...}

send_message(spawn_id="abc", message="继续分析")
  → 向存活 agent 发消息，返回 DelegationSummary

close_agent(spawn_id="abc")
  → 显式关闭 agent，释放资源
```

### 3.5 配置

```json
{
  "subagent": {
    "live_agent_ttl_seconds": 300,
    "max_live_agents_per_run": 3
  }
}
```

### 3.6 与现有模式的关系

| 模式 | 对话历史 | Agent 存活 | 工具 |
|------|---------|-----------|------|
| EPHEMERAL | 不保留 | spawn→run→销毁 | spawn_agent + check_spawn_result |
| LONG_LIVED | 累积 | spawn→run→IDLE→send→run→IDLE→close | spawn_agent + send_message + close_agent |

EPHEMERAL 行为完全不变。

## 四、实现清单

| 文件 | 改动 |
|------|------|
| `models/subagent.py` | SubAgentStatus 加 IDLE |
| `infra/config.py` | SubAgentConfig 加 live_agent_ttl_seconds + max_live_agents_per_run |
| `subagent/runtime.py` | _LiveAgent 数据类 + _live_agents 池 + send_message() + cleanup + TTL |
| `subagent/delegation.py` | DelegationExecutor 加 send_to_subagent() + close_subagent() |
| `tools/builtin/spawn_agent.py` | 新增 send_message + close_agent 工具 |
| `tools/schemas/builtin_args.py` | SendMessageArgs + CloseAgentArgs |
| `tools/executor.py` | _route_subagent 新增 send_message/close_agent 路由 |
| `tools/builtin/__init__.py` | 注册新工具 |
| `agent/prompt_templates.py` | 教 LLM 使用 send_message |
| `agent/coordinator.py` | run 结束时 cleanup live agents |
| `protocols/core.py` | SubAgentRuntimeProtocol 加 send_message |
| `tests/test_long_lived.py` | 完整测试 |

## 五、边界约束

1. IDLE agent 不参与父的迭代循环——只在被 send_message 唤醒时执行
2. IDLE agent 的 session 受 max_context_tokens 约束——超长时自动压缩
3. 同一 spawn_id 不能并发 send_message（IDLE 时才接受）
4. LONG_LIVED 的 spawn_agent(wait=true) 执行完后返回 IDLE 而非 COMPLETED
5. LONG_LIVED 的 spawn_agent(wait=false) + check_spawn_result 仍可用（异步模式）
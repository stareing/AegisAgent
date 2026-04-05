# OpenClaw 分析与移植报告

## 1. OpenClaw 概览

**定位**: 多渠道 AI 网关产品 (Multi-channel AI gateway with extensible messaging integrations)

| 维度 | 数据 |
|------|------|
| 语言 | TypeScript ESM, Node 22+ |
| 总代码量 | ~918K LOC (不含测试) |
| src/ 生产代码 | 528K LOC / 3,012 文件 |
| src/ 测试代码 | 465K LOC / 2,080 文件 |
| extensions/ 插件 | 228K LOC / 20+ 消息渠道 |
| apps/ 客户端 | 122K LOC (macOS/iOS/Android) |
| ui/ | 39K LOC |

### 启动方式 (三层入口链)

```
openclaw.mjs (Node版本检查 + 编译缓存)
  └─> dist/entry.js (进程标题/环境/警告/respawn)
      └─> cli/run-main.ts → runCli() (Commander.js 命令树)
```

**特点**: 重度懒加载 — 几乎所有模块 `await import()` 按需加载, 冷启动仅加载最小路径.

### src/ 核心模块 Top 10

| 模块 | 行数 | 职责 |
|------|------|------|
| agents | 101,883 | AI Agent 运行时 (最大模块) |
| gateway | 50,839 | 网关服务器 |
| infra | 46,466 | 基础设施 |
| config | 46,241 | 配置系统 |
| auto-reply | 38,861 | 自动回复引擎 |
| commands | 38,148 | 命令处理 |
| cli | 31,411 | CLI 框架 |
| plugins | 31,014 | 插件系统 |
| browser | 17,116 | 浏览器集成 |
| channels | 15,484 | 多渠道消息 |

### 插件生态 Top 5

Discord (31K), Telegram (23K), Matrix (22K), Feishu (16K), Slack (14K)

---

## 2. OC Agent 模块架构 (~102K LOC)

### 2.1 核心运行时 (三层)

| 层 | 文件 | 职责 |
|----|------|------|
| Entry | `pi-embedded.ts` | Facade: runEmbeddedPiAgent, subscribe, compact |
| Run | `pi-embedded-runner/run.ts` | 重试逻辑, 指数退避, auth 刷新, compaction 诊断, fallback |
| Subscribe | `pi-embedded-subscribe.ts` | Token→Message 流式, `<think>`/`<final>` tag 解析, block reply 分块 |

### 2.2 流式架构

**Think Tag 解析**:
```typescript
const THINKING_TAG_SCAN_RE = /<\s*(\/?)\s*(?:think(?:ing)?|thought|antthinking)\s*>/gi;
```
- 支持多种 tag 变体 (`<think>`, `<thinking>`, `<thought>`, `<antthinking>`)
- 跨 chunk 的有状态追踪 (`blockState.thinking`, `blockState.final`)

**Soft Chunk 段落偏好**:
```typescript
type BlockReplyChunking = {
  minChars: number;       // 最小字符数
  maxChars: number;       // 最大字符数
  breakPreference?: "paragraph" | "newline" | "sentence";
  flushOnParagraph?: boolean;  // 段落边界优先
};
```
- 优先在 `\n\n` 段落边界分块
- 不可分割 fenced code blocks (强制分割时关闭/重新打开 fence)

**流式回调**:
- `onBlockReply` — 文本块就绪
- `onReasoningStream` — thinking 内容流
- `onReasoningEnd` — `</think>` 处理完成
- `onBlockReplyFlush` — 工具执行前 flush
- `onPartialReply` — 中间流式回复
- `onAssistantMessageStart` — 最早的 "writing" 信号

### 2.3 工具系统

**11 类工具 / 4 个 Profile**:

| Profile | 包含工具 |
|---------|---------|
| minimal | session_status only |
| coding | Files, Runtime, Web, Memory, Sessions, Agents |
| messaging | Sessions subset (send, history, list) |
| full | 所有工具 (无限制) |

**Bash/PTY 执行**:
- `createExecTool()`: 前台/后台/PTY 三种模式
- 安全管线: 脚本预检 → 安全二进制验证 → 审批请求 → 沙箱执行 → 输出截断
- `ProcessTool`: list/poll/log/write/send-keys/kill/clear/remove

### 2.4 Model Failover & Cooldown

**指数冷却**:
```
errorCount 1 → 60s
errorCount 2 → 300s (5m)
errorCount 3 → 1500s (25m)
errorCount 4+ → 3600s (1h max)
```

**Probe 策略**:
- 瞬态故障 (rate_limit, overloaded, unknown) → 允许探测
- 永久故障 (auth, billing, format, model_not_found) → 跳过探测

**Auth Profile 轮换**: 按 lastUsed 排序, 过滤 cooldown/不可用, 自动过期重置.

### 2.5 上下文管理

| 参数 | 值 | 说明 |
|------|------|------|
| DEFAULT_CONTEXT_TOKENS | 200K | 保守默认 |
| Anthropic 特殊 | 1M | 大上下文 |
| BASE_CHUNK_RATIO | 0.4 | 基础压缩比 |
| MIN_CHUNK_RATIO | 0.15 | 最小压缩比 |
| SAFETY_MARGIN | 1.2x | 安全余量 |
| SUMMARIZATION_OVERHEAD | 4096 tokens | 保留给 prompt/reasoning |

**标识符保留**: Strict policy — UUID, token, IP, URL 在压缩中原样保留.

### 2.6 向量记忆

| 特性 | 实现 |
|------|------|
| 混合搜索 | Vector (0.7 权重) + FTS (0.3 权重) |
| MMR 重排 | λ=0.7, Jaccard 相似度 |
| 时间衰减 | 指数衰减, 半衰期 30 天 |
| 分块 | 400 tokens, 80 overlap |
| Embedding 提供者 | OpenAI, Gemini, Voyage, Mistral, Ollama, local |

### 2.7 沙箱

```bash
docker run --rm \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 256 \
  --memory 512m \
  --cpus 1.5 \
  --network none \
  --tmpfs /tmp \
  --ulimit nofile=1024:2048 \
  -v $workspace:/workspace \
  $image sh -c "$command"
```

工作区挂载模式: `rw` | `ro` | `none`, 路径安全: 符号链接逃逸防护.

---

## 3. OC 插件/扩展系统

### 3.1 Plugin Manifest Schema

文件: `extensions/<id>/openclaw.plugin.json`

```json
{
  "id": "discord",
  "configSchema": { "type": "object" },
  "enabledByDefault": false,
  "kind": "memory" | "context-engine",
  "channels": ["discord"],
  "providers": ["discord-provider"],
  "skills": ["discord-skill"],
  "providerAuthEnvVars": { "discord": ["DISCORD_BOT_TOKEN"] },
  "uiHints": { "token": { "label": "Bot Token", "sensitive": true } }
}
```

### 3.2 Plugin 生命周期

```
Discovery → Manifest Validation → Dynamic Import (Jiti) → Activation
                                                           ├── register.tool(factory)
                                                           ├── register.hook(handler)
                                                           ├── register.provider(plugin)
                                                           ├── register.channel(plugin)
                                                           └── register.service(...)
```

**PluginRecord** 运行时状态:
- id, name, version, format, kind, source, origin
- enabled, status: "loaded" | "disabled" | "error"
- toolNames[], hookNames[], channelIds[], providerIds[]

### 3.3 Plugin Hook 系统 (18+ 类型)

| Hook | 触发时机 |
|------|---------|
| before-agent-start | Agent 启动前 |
| before-model-resolve | 模型选择前 |
| before-prompt-build | Prompt 构建前 |
| llm-input / llm-output | LLM 调用前后 |
| before-tool-call / after-tool-call | 工具调用前后 |
| inbound-claim | 消息路由 |
| message-received / sending / sent | 消息生命周期 |
| session-start / session-end | 会话生命周期 |
| subagent-spawning / spawned / ended | 子 Agent 生命周期 |
| after-compaction | 上下文压缩后 |

### 3.4 Channel Plugin Contract (18 个 Adapter 类别)

config, setup, auth, pairing, security, allowlist, messaging, streaming, threading, outbound, status, gateway, directory, resolver, actions, groups, mentions, lifecycle

### 3.5 Tool Factory Pattern

```typescript
register.tool((context: PluginToolContext) => [{
  name: "discord_send",
  description: "Send a Discord message",
  handler: async (params) => { /* sessionId, agentId available from context */ }
}])
```

工具按 session 创建, 携带 (sessionId, agentId, workspaceDir) 上下文.

### 3.6 Skills vs Plugins

| 维度 | Skills | Plugins |
|------|--------|---------|
| 位置 | `skills/<id>/SKILL.md` | `extensions/<id>/` |
| 格式 | YAML frontmatter + Markdown | plugin.json + TypeScript |
| 功能 | 执行指导 (metadata + instructions) | 完整代码扩展 |
| 集成 | 注入到 Agent 上下文 | 注册 hooks/tools/providers/channels |

---

## 4. OC 多 Agent 系统

### 4.1 ACP (Agent Communication Protocol)

控制平面驱动 (非 peer-to-peer 邮箱):
- Manager: 会话生命周期, 运行时缓存, 轮次队列
- Runtime: 抽象后端接口
- Persistent Bindings: 线程绑定持久化

### 4.2 Spawn 模型

| 模式 | 说明 |
|------|------|
| run | 一次性执行 (oneshot) |
| session | 持久线程绑定 (long-lived) |

**Sandbox**: `inherit` (继承父级) | `require` (强制沙箱)
**Depth Limits**: 防止无限递归和资源耗尽

### 4.3 SubAgent Registry

- In-memory `Map<runId, SubagentRunRecord>`
- Announce 重试队列 (指数退避)
- 生命周期事件 (start, end, error)
- 父子血缘追踪

### 4.4 Session Scope 隔离

- `"children"` (orchestrator): 可控制其他会话
- `"none"` (leaf): 禁止控制操作, 防止越权

---

## 5. Python 框架 vs OC 差异对比

| 能力 | OC | Python Framework | 差异评估 |
|------|----|--------------------|----------|
| **Agent 循环** | pi-embedded-runner (重试/failover/流式) | RunCoordinator → AgentLoop → IterationResult | Python 更干净 (零写入循环) |
| **状态管理** | 隐式流式 state | 显式三层: Coordinator/StateController/PolicyResolver | **Python 更优** |
| **工具系统** | 11类/4 profile/pipeline 包装 | 统一路由 local/mcp/a2a/subagent + security chain | 思路一致 |
| **上下文** | Token-aware compaction (40% ratio, 1.2x margin) | ContextEngineer (Source→Builder→Compressor, 85% threshold) | OC token 管理更精细 |
| **模型适配** | 动态 catalog + cooldown failover | 20+ adapter (Protocol→Base→Default) + fallback chain | OC 更面向运维 |
| **子 Agent** | run/session + registry 持久化 | 15 态状态机 + 3 种收集策略 + HITL + 事件通道 | **Python 更完整** |
| **记忆** | vector + hybrid + MMR + temporal decay | Rule-based extraction + 4 store backends | OC 检索更智能 |
| **Skills** | SKILL.md + bundled allowlist | SKILL.md + config-based + keyword trigger | 类似 |
| **Hooks** | 18+ plugin hooks | 31 HookPoints + OBSERVATION/DECISION 分离 | **Python 更系统化** |
| **流式** | `<think>` tag + block reply + soft chunk | StreamSink + run_stream | OC 远更复杂 |
| **安全** | Docker sandbox + workspace guard + owner-only | CapabilityPolicy + confirm handler | OC 有真正沙箱 |
| **插件** | 完整 SDK (manifest + registry + lifecycle + 18 hooks) | 骨架 (manifest + loader + lifecycle) | OC 远更成熟 |

---

## 6. 已移植的 OC 优势 (4 Phase)

### Phase 1: Plugin/Extension System (OC-Compatible)

| 组件 | 文件 | 说明 |
|------|------|------|
| PluginManifest 扩展 | `models/plugin.py` | kind, channels, providers, skills, ui_hints, min_host_version |
| OC 兼容模型 | `models/plugin.py` | PluginKind (6种), PluginConfigUiHint, PluginDiagnostic, PluginRecord |
| Hook Points | `models/hook.py` | +9 OC 等效: LLM_INPUT/OUTPUT, SESSION_START/END, SUBAGENT_SPAWNING/SPAWNED/ENDED, AFTER_COMPACTION, MODEL_RESOLVE |
| Tool Factory | `plugins/tool_factory.py` | PluginToolContext (frozen) + PluginToolFactory protocol |
| Manifest 发现 | `plugins/loader.py` | plugin.json 支持, camelCase→snake_case 映射, LRU 缓存 |
| 状态追踪 | `plugins/registry.py` | PluginRecord 观测, 诊断记录, list_records() |
| 错误隔离 | `plugins/lifecycle.py` | 插件失败不崩溃宿主, 错误记录到 PluginRecord |
| Protocol 扩展 | `plugins/protocol.py` | +get_tool_factories(), +get_providers(), +get_channels() |
| Config | `infra/config.py` | PluginConfig (dirs, enabled/disabled lists, plugin_configs, auto_discover) |
| 接入 | `entry.py` | setup() 中完整插件发现→加载→启用 |

### Phase 3: Model Failover & Circuit Breaker

| 组件 | 文件 | 说明 |
|------|------|------|
| 故障分类 | `adapters/model/failover_types.py` | FailoverReason (9级), severity 排序, classify_error() |
| 断路器 | `adapters/model/circuit_breaker.py` | 指数冷却 60s→300s→1500s→3600s, 探测策略, 自动恢复 |
| 智能 Fallback | `adapters/model/fallback_adapter.py` | CircuitBreaker 集成, 跳过冷却, 错误分类 |
| Config | `infra/config.py` | circuit_breaker_enabled, cooldown_tiers, probe_transient |

### Phase 4: Tool Sandbox & Enhanced Execution

| 组件 | 文件 | 说明 |
|------|------|------|
| Sandbox Protocol | `tools/sandbox/protocol.py` | SandboxConfig (frozen, OC 安全加固), SandboxResult |
| Container 实现 | `tools/sandbox/container_sandbox.py` | Docker/Podman --read-only --cap-drop ALL --no-new-privileges |
| 路径安全 | `tools/sandbox/path_security.py` | 符号链接逃逸防护, 边界验证 |
| 进程注册 | `tools/process_registry.py` | register/poll/kill/list, TTL 清理 |
| Config | `infra/config.py` | sandbox_enabled/runtime/image/memory/pids/network/workspace_mount |

### Phase 5: Context Window Intelligence

| 组件 | 文件 | 说明 |
|------|------|------|
| Token 预算 | `context/token_budgets.py` | 20+ provider 上下文窗口, 模型级覆盖, guard 评估 |
| 标识符保留 | `context/identifier_preservation.py` | UUID/IP/URL/Token/Path 提取, LLM 保护指令 |
| 自适应压缩 | `context/compressor.py` | AdaptiveCompactionConfig (0.4 base, 1.2x margin), 动态 ratio |
| Summarizer | `context/summarizer.py` | extra_instructions 支持 (标识符保留注入) |
| Config | `infra/config.py` | adaptive_compaction, identifier_preservation, provider_context_window_override |

---

## 7. 未移植项 (按需后续)

| 特性 | 优先级 | 原因 |
|------|--------|------|
| 向量记忆检索 (Phase 2) | 中 | 用户要求跳过 |
| 流式 `<think>` tag 解析 | 低 | Python 端流式已完整, 暂无需求 |
| 完整 Channel Plugin Contract | 低 | 非核心, 产品向 |
| ACP 控制平面 | 低 | Python 端邮箱协议更完整 |
| macOS/iOS/Android 客户端 | N/A | 不同产品形态 |

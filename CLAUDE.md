# AI Agent Framework - CLAUDE.md

* **单一职责**：一个模块、类、函数只负责一类清晰职责。
* **禁止重复造轮子**：优先复用成熟开源方案，非核心能力不自研。
* **导入前置**：`import` 原则上统一放在文件头部。
* **最小暴露**：非公开能力默认私有，减少无必要的对外接口。
* **命名清晰**：名称必须表达职责，避免缩写和含糊命名。
* **显式优于隐式**：禁止依赖隐藏副作用和隐式状态流转。
* **类型优先**：公开接口必须补全类型标注。
* **数据与行为分离**：数据模型不承载复杂业务逻辑。
* **面向接口编程**：依赖抽象，不直接耦合具体实现。
* **默认不可变**：能不用可变状态就不用可变状态。
* **异常要分类**：不要抛裸异常，错误类型要明确。
* **失败可解释**：错误信息必须可读、可定位、可处理。
* **函数尽量短小**：单个函数尽量只完成一个完整动作。
* **避免深层嵌套**：优先早返回，减少多层 `if/else`。
* **禁止魔法值**：重复使用的常量必须提取命名。
* **配置外置**：可变参数放配置，不写死在逻辑中。
* **副作用集中**：I/O、网络、数据库调用集中在边界层。
* **注释解释原因**：注释优先说明"为什么"，不是重复"做什么"。
* **兼容性优先**：公共接口变更必须考虑向后兼容。
* **测试友好**：设计必须便于 mock、替换和单元测试。
* **边界清晰**：跨层调用必须通过正式接口，禁止越层访问。
* **一处定义**：同一规则、常量、协议只保留一个权威定义。
* **记忆进度**：热更新CLAUDE.md技能修复摘要
* **代码审核**：将由codex排查代码是否和需求一致

## Project Overview
Offline-first, extensible AI Agent Framework in Python 3.11+ / pydantic v2.
Protocol → Base → Default three-layer pattern. Tech: structlog, blinker, litellm, SQLite, MCP SDK, A2A SDK.

## Architecture Layers
```
Entry → entry.py, cli.py | Agent → agent/ | SubAgent → subagent/ | Tools → tools/
Memory → memory/ | Context → context/ | Protocols → protocols/ | Models → models/
Adapters → adapters/model/ | Infra → infra/
```

## Completed Tasks
- **L1-8** 全模块骨架 (infra/models/protocols/adapters/tools/memory/context/agent)
- **#11-16** ReAct Agent, SubAgent Runtime, MCP/A2A Client, Entry+CLI, Integration
- **#17-27** 多智能体协调 + v2.4架构 (Policy/Config/SessionState/AgentLoop/Skill/MemoryScope)
- **#29-37** 多模型适配(10+) + Skill系统 + 交互终端main.py
- **#38-39** 终止条件6层闭环 + 全链路日志50+事件
- **#40-52** 架构审查+收口 (v2.5.1→v2.6.5): RunCoordinator三层拆分, Hook/Decision分离, TerminationKind, CommitSequencer, SubAgent状态机, SessionSnapshot, 重试版本链, 架构守卫43项, 700 tests
- **#53** 记忆+上下文闭环修复 (详见下方)

### #53 记忆+上下文管理闭环修复

**消息顺序修复**
- task 作为 user 消息在 run 开始时写入 SessionState (coordinator.py)
- 移除 current_input 末尾重复注入 (engineer.py)
- 修复前: `[system] → [assistant+tool] → [tool_result] → [user]` (LLM 误判为新请求)
- 修复后: `[system] → [user] → [assistant+tool] → [tool_result]` (标准 API 格式)

**压缩策略精简**
- 移除 SLIDING_WINDOW / TOOL_RESULT_SUMMARY, 仅保留 LLM incremental summarization
- 移除 CompressionStrategy 枚举, compressor 不再有 lossy fallback
- 删除 memory/policies.py 重复定义

**策略闭环接通**
- MemoryPolicy: coordinator → memory_manager.apply_memory_policy() (memory_enabled/auto_extract/max_in_context 生效)
- ContextPolicy: coordinator → context_engineer.apply_context_policy() (allow_compression 生效)
- MemoryQuota: entry.py → set_quota() (max_items_per_user/max_content_length/max_tags 执行)
- 删除 ContextPolicy 死字段 (prefer_recent_history/max_session_groups)
- 协议补全: MemoryManagerProtocol + ContextEngineerProtocol 增加 policy 方法
- models/__init__.py 导出 MemoryPolicy/MemoryQuota/ContextPolicy

**Config → Policy 优先级统一**
- entry.py:_bind_config_policies() 将 FrameworkConfig 值绑定到 agent 的 get_memory_policy()/get_context_policy()
- 解决: BaseAgent 默认策略覆盖全局配置的问题
- 链路: FrameworkConfig → agent.get_*_policy() → RunPolicyResolver → coordinator → apply_*_policy()

**其他修复**
- 异常路径 record_turn: except 分支也执行 CommitDecision (契约一致)
- XML 转义: source_provider 所有用户可控值通过 html.escape() (memory/skill/runtime_info)
- note 工具接入 memory_manager.remember() 持久化
- SharedWriteMemoryManager.record_turn 不再误委托 parent
- user_id 参数贯通 coordinator.run() → begin_run_session()

### Bug Fixes
- 消息顺序: user 消息在 tool_result 之后导致 LLM 重复调用工具
- 策略断裂: MemoryPolicy/ContextPolicy 解析后未传给执行层
- Config 覆盖: run 级默认策略反向覆盖 FrameworkConfig 值
- 双定义: memory/policies.py 与 models/agent.py 的 MemoryPolicy 冲突
- XML 注入: memory title/content 直接拼接 XML 标签无转义

## Key Design Patterns

### 核心架构
- **RunCoordinator三层**: Coordinator(编排) + StateController(状态) + PolicyResolver(配置)
- **AgentLoop零写入**: 返回 IterationResult, 不直接修改 AgentState/SessionState
- **MessageProjector**: 格式化与状态分离, 返回 message 列表供 RunStateController 提交
- **SessionSnapshot只读**: 上下文层消费冻结快照, 不直接读可变 SessionState

### 策略与配置
- **策略解释权唯一**: ContextPolicy→ContextEngineer, MemoryPolicy→MemoryManager, CapabilityPolicy→授权链
- **ResolvedRunPolicyBundle**: RunPolicyResolver 唯一产出, frozen 后不可改
- **Config→Policy链路**: FrameworkConfig → _bind_config_policies() → agent.get_*_policy() → apply_*_policy()
- **配额硬软语义**: 硬(超出→拒绝) vs 软(超出→降级), 每个配额有唯一 Owner
- **MemoryQuota执行**: content_length/tags_count/max_items 三层检查在 remember() 中

### 工具与权限
- **工具命名**: `local::<name>`, `mcp::<srv>::<name>`, `a2a::<alias>::<name>`, `subagent::spawn_agent`
- **权限链**: schema导出(可见性) → is_tool_allowed()(安全) → on_tool_call_requested()(agent hook)
- **Hook/Decision分离**: 观察hooks→无返回值, 决策接口→结构化 Decision 模型
- **batch_execute顺序**: asyncio.gather 保证结果按输入顺序返回

### 上下文管理
- **Context 组装**: System Core + Skill Addon → Frozen Prefix → + Memory Block → + Session History
- **压缩**: 仅 LLM incremental summarization, frozen summary 跨迭代复用, 失败返回原始 groups
- **XML 转义**: source_provider 对所有用户可控值执行 html.escape()
- **Frozen Prefix**: system_core + skill_addon 缓存, hash 验证, 跨迭代复用

### 记忆管理
- **会话成对**: begin_run_session / end_run_session (finally), record_turn → CommitDecision
- **记忆控制**: MemorySourceContext 审计, CandidateSource+Confidence 写入优先级
- **SubAgent 记忆**: Isolated/InheritRead/SharedWrite, spawn 时冻结快照, SharedWrite 强制 subagent 源标记
- **note 工具**: 接入 memory_manager.remember(), 持久化到 SQLite

### 终止与错误
- **终止6层**: LLM_STOP → MAX_ITERATIONS → OUTPUT_TRUNCATED → ERROR(3次) → USER_CANCEL → timeout
- **TerminationKind**: NORMAL / ABORT / DEGRADE, 派生自 StopReason
- **stuck loop**: 连续相同工具调用检测 → 提取上次结果 → 强制停止

### 子Agent
- **Factory纯装配**: 消费已解析配置, 不解释策略
- **SubAgentScheduler/Runtime分离**: Scheduler(排队/配额) vs Runtime(执行/cancel)
- **SubAgentStatus**: COMPLETED/FAILED/CANCELLED/REJECTED/DEGRADED 统一状态机

### 不可变与边界
- **不可变模型**: EffectiveRunConfig frozen, ToolMeta frozen, FrozenPromptPrefix frozen
- **iteration_history**: append-only, 不可删除/替换/重排, 压缩不影响
- **Framework vs Integration**: Core(runtime/tools/context/memory) vs Integration(auth/UI/DTOs)
- **None语义**: None="不存在", 失败用 error 对象, 空集合用 []

## Commands
```bash
pip install -e ".[dev]"           # Install
pytest tests/                     # Tests (700 passed)
python -m agent_framework.main    # Interactive (Mock, no API key)
python -m agent_framework.main --config config/deepseek.json  # Real model
python run_demo.py                # Demo
```

## File Conventions
- pydantic v2 BaseModel / pydantic-settings BaseSettings
- TYPE_CHECKING for forward refs, runtime_checkable Protocols
- @tool decorator auto-detects async

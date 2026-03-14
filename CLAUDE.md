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
* **注释解释原因**：注释优先说明“为什么”，不是重复“做什么”。
* **兼容性优先**：公共接口变更必须考虑向后兼容。
* **测试友好**：设计必须便于 mock、替换和单元测试。
* **边界清晰**：跨层调用必须通过正式接口，禁止越层访问。
* **一处定义**：同一规则、常量、协议只保留一个权威定义。
* **记忆进度**：热跟新CLAUDE.md
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
- **L1-8** infra/models/protocols/adapters/tools/memory/context/agent 全模块完成
- **#11-16** ReAct Agent, SubAgent Runtime, MCP Client, A2A Client, Entry+CLI, Integration wiring
- **#17-22** 多智能体协调: Scheduler API, Executor路由, spawn_seed, 派生权限, 递归防护
- **#23-27** v2.4架构: ContextPolicy/MemoryPolicy/EffectiveRunConfig, SessionState写锁, AgentLoop最小依赖, Skill反激活, MemoryScope快照
- **#29-30** 多模型适配: OpenAI/Anthropic/Google原生SDK + DeepSeek/豆包/通义/智谱/MiniMax/Custom (OpenAI-compatible)
- **#31** 602 tests 全模块覆盖
- **#32-37** Skill系统(API/配置/CLI) + 交互终端main.py + 入口点
- **#38-39** 终止条件6层闭环(LLM_STOP/MAX_ITER/TRUNCATED/ERROR/CANCEL/TIMEOUT) + 全链路日志50+事件
- **#40** 架构审查7项: RunCoordinator三层拆分, 消息投影规则, Policy消费边界, EffectiveRunConfig冻结, CapabilityPolicy双执行, remember()审计, 记忆治理拦截
- **#41** 架构审查6项: iteration_history append-only, ToolResult序列化边界, 日志≠状态, Artifact生命周期, 确认决策分层, ScopedRegistry边界, 委派失败统一语义
- **#42** Bug(dedup_guard)+7项: Message metadata边界, ToolMeta冻结, 记忆抽取边界, MemoryCandidate置信度, 格式稳定性, Factory反膨胀, Framework/Integration边界
- **#43** 架构审查7项: 对象作用域4级分类, SkillRouter纯目录化(active_skill→run-scoped), ContextEngineer只读合约, batch_execute顺序保证, ErrorCode统一注册表, 配额硬软语义, None语义规范
- **#44** v2.5.1边界修复7项: RunStateController持有active_skill+activate/deactivate, MessageProjector提取(格式化与状态分离), AgentLoopDeps最小依赖(frozen dataclass), 策略解释权唯一化, AuthorizationDecision结构化授权, SubAgentConfigOverride强类型白名单(替代dict), 实现红线测试(8条断言)
- **#45** v2.5.2边界修复11项: BaseAgent hook/decision分离(StopDecision/ToolCallDecision/SpawnDecision), 终止语义三分类(TerminationKind: NORMAL/ABORT/DEGRADE), 委派返回分层文档, Session/audit对齐(iteration_id in metadata), CommitSequencer串行提交, 配额所有权表, DTO边界文档, EventBus观察边界, Integration旁路禁止, None/error强化, 红线测试(11条断言)
- **#46** v2.5.3必修+建议修: AgentLoop零状态写入(status/tokens/history全部移至RunStateController), apply_iteration_result统一入口, set_status/add_tokens/snapshot/append_user_message, SubAgentRawResult(Layer0内部详情), 红线测试(14条断言)
- **#47** v2.6.1收口5项: ResolvedRunPolicyBundle(config唯一源), Decision模型+source字段, AgentRunResult.termination_kind, tool_category_whitelist交集语义(禁止扩权), 流式输出边界(ModelChunk禁入SessionState), 红线测试(13条断言)
- **#48** v2.6.3收口4项: SubAgentScheduler/Runtime所有权分离(SubAgentTaskRecord+active_children唯一真相源), TransactionGroupIndex(禁止ContextSourceProvider重建事务组), MemoryManager会话生命周期(begin_run_session/end_run_session成对+CommitDecision+RunSessionOutcome), RuntimeIdentityBundle(ID归属表+内外ID分离), 红线测试(23条断言)
- **#49** v2.6.4收口4项: 并发工具副作用提交串行化(ToolExecutionOutcome+ToolCommitSequencer按input_index排序), 委派统一状态机(SubAgentStatus: COMPLETED/FAILED/CANCELLED/REJECTED/DEGRADED+error_code映射), 上下文层只读快照(SessionSnapshot冻结视图+RunStateController.session_snapshot()), 子Agent装配器纯装配(ResolvedSubAgentRuntimeBundle+Factory禁止解释策略), 红线测试(21条断言)
- **#50** v2.6.5收口4项: 自动重试幂等边界(RetrySafety+RetryDecision, retryable≠idempotent, 无幂等键禁止自动重放), checkpoint/resume正式立场(RunCheckpoint占位, 当前不支持resume, SessionState/日志/事件不可作恢复源), 事件投递语义(EventEnvelope+event_id幂等+尽力投递+订阅方必须幂等), 重试版本链(IterationAttempt+TransactionGroupAttempt, retry不覆盖原记录+parent链关联), 红线测试(18条断言)
- **#51** 架构守卫套件(test_architecture_guard.py 43项): 反旁路扫描(SessionState写端口/AgentLoop零写/Policy解释权/TransactionGroupIndex消费/Factory纯装配), 故障注入(模型500/工具部分失败/子Agent超时/memory提交失败/session结束失败/事件重复/取消/超时), 数据流不变量(iteration_history只增/迭代ID注入/快照冻结/提交排序/重试版本链/白名单交集/映射完备/begin-end配对)

- **#52** 全面优化修复: P2错误处理(scheduler/delegation异常日志), P3死代码(ResolvedRunPolicyBundle未用导入), P4架构接入(SessionSnapshot传递上下文层+CommitDecision消费审计+memory_scope返回CommitDecision), P5性能(remember()单次遍历), 内置工具测试(filesystem+system 25项)

### Bug Fixes
- RunCoordinator初始消息写入改用RunStateController(v2.5.1写端口合规)
- SubAgentFactory构造修复, 工具类别阻止, MemoryScope extract_candidates, build_spawn_seed委托
- SmartMockModel累积bug, structlog噪音, _rl_wrap regex崩溃, parent_run_id传递, dedup_guard ValidationError

## Key Design Patterns
- **工具命名**: `local::<name>`, `mcp::<srv>::<name>`, `a2a::<alias>::<name>`, `subagent::spawn_agent`
- **Context 5-slot**: System Core → Skill Addon → Saved Memories → Session History → Current Input
- **权限链**: schema导出(可见性) → ToolExecutor.is_tool_allowed()(安全) → on_tool_call_requested()
- **RunCoordinator三层**: Coordinator(编排) + StateController(状态) + PolicyResolver(配置)
- **不可变模型**: EffectiveRunConfig frozen, ToolMeta frozen (注册后不可改)
- **记忆控制**: MemorySourceContext审计(user/agent/subagent/admin), CandidateSource+Confidence写入优先级, SharedWrite强制subagent标记
- **终止6层**: LLM_STOP → MAX_ITERATIONS → OUTPUT_TRUNCATED → ERROR(3次) → USER_CANCEL → run_timeout_ms
- **子Agent**: Factory强制allow_spawn_children=False, ISOLATED/INHERIT_READ/SHARED_WRITE(spawn快照), DelegationErrorCode统一错误码
- **确认分层**: force_confirm_categories > ToolMeta.require_confirm > 默认不确认
- **多模型**: LiteLLM/OpenAI/Anthropic/Google + 6国产(OpenAI-compatible), adapter_type选择
- **格式稳定**: ContextSourceProvider确定性输出, iteration_history append-only审计轨迹
- **边界**: Framework Core(runtime/tools/context/memory) vs Integration(auth/UI/DTOs), 日志仅观测不影响业务
- **对象作用域**: 进程级(Config/Catalog/Logger) → Agent级(Deps/Registry/Adapter) → Run级(AgentState/SessionState/EffectiveRunConfig) → SubAgent级(scoped registry/memory view)
- **SkillRouter纯目录**: router只负责注册+检测, active_skill由RunStateController持有(run-scoped), 杜绝并发串状态
- **ContextEngineer只读**: 不改SessionState/MemoryRecord/AgentState, 压缩结果仅影响本次LLM请求
- **batch_execute顺序保证**: 结果按输入请求顺序返回(asyncio.gather), 不按完成顺序
- **ErrorCode统一注册表**: 三层(通用/工具/委派), 新错误码必须追加注册, 禁止自由命名
- **配额硬软语义**: 硬限制(超出→拒绝: max_iterations/spawn数/递归) vs 软限制(超出→降级: token budget/并发队列/memory条数)
- **None语义**: None="字段不存在", 失败用error对象, 空集合用[], "未生成"仅限内部对象
- **MessageProjector**: 格式化与状态写入分离, 返回message列表, 不直接写SessionState
- **AgentLoopDeps**: frozen dataclass(model_adapter+tool_executor), 禁止传入完整AgentRuntimeDeps
- **AuthorizationDecision**: 结构化授权结果(allowed+reason+source_layer), 禁止裸bool
- **SubAgentConfigOverride**: 强类型白名单(model_name/temperature/system_prompt_addon), 禁止dict注入
- **策略解释权唯一**: ContextPolicy→ContextEngineer, MemoryPolicy→MemoryManager, CapabilityPolicy→授权链, 其他模块禁读字段
- **Hook/Decision分离**: 观察hooks(on_before_run等)→无返回值, 决策接口(should_stop/on_tool_call_requested/on_spawn_requested)→结构化Decision模型
- **TerminationKind三分类**: StopSignal.termination_kind派生属性, NORMAL(LLM_STOP/CUSTOM) / ABORT(ERROR/USER_CANCEL) / DEGRADE(MAX_ITERATIONS/OUTPUT_TRUNCATED)
- **CommitSequencer**: asyncio.Lock包装, 保证并发结果串行提交到SessionState
- **配额所有权表**: 每个配额有唯一Owner模块负责执行, 禁止跨模块读配额自行执行
- **iteration_id链接**: MessageProjector在metadata中注入iteration_id, 关联SessionState消息↔iteration_history审计轨迹
- **ResolvedRunPolicyBundle**: RunPolicyResolver唯一产出, RunCoordinator只消费, frozen后不可改
- **tool_category_whitelist交集语义**: 白名单只能收窄(blocked∩whitelist), 不能扩权绕过CapabilityPolicy
- **流式边界**: ModelChunk仅供UI输出, 不入SessionState; 中断不写半成品; 只有最终ModelResponse落盘
- **AgentRunResult.termination_kind**: 派生属性区分stop/abort/degrade, 审计日志必须可区分
- **SubAgentScheduler/Runtime分离**: Scheduler负责排队/配额/task_id分配, Runtime负责执行/active_children真相源/cancel执行, 禁止两层同时持有active child集合
- **SubAgentTaskRecord**: 统一任务记录(QUEUED→SCHEDULED→RUNNING→COMPLETED/FAILED/CANCELLED), task_id由Scheduler分配, child_run_id由Runtime分配
- **TransactionGroupIndex**: 预计算事务组索引, ContextSourceProvider只消费不重建, 缺失metadata降级为"不可安全裁剪"而非重建
- **MemoryManager会话成对**: begin_run_session/end_run_session必须成对, end_run_session必须finally执行, record_turn返回CommitDecision
- **CommitDecision/RunSessionOutcome**: 结构化提交决策(committed+reason+source), 结构化终止描述(status+termination_kind+audit_ref)
- **RuntimeIdentityBundle**: 内核ID(run_id/run_session_id/iteration_id)与外部ID(external_session_id/request_id/user_id)分离, 内核可无外部ID独立运行
- **工具并发副作用提交**: 并发只允许计算阶段, 可观察副作用(session写/artifact登记/审计)经ToolCommitSequencer按input_index串行化提交
- **ToolExecutionOutcome**: 结构化工具执行结果(tool_call_id+input_index+result+artifact_refs+side_effect_refs)
- **SubAgentStatus统一状态机**: COMPLETED/FAILED/CANCELLED/REJECTED/DEGRADED, 本地subagent与A2A统一, error_code→status映射表
- **SessionSnapshot只读快照**: 上下文层不直接消费可变SessionState, RunStateController产出冻结快照, 同一次上下文构建绑定单一snapshot
- **SubAgentFactory纯装配**: Factory只消费已解析配置(ResolvedSubAgentRuntimeBundle), 不解释MemoryScope/CapabilityPolicy/EffectiveRunConfig/quota
- **自动重试幂等边界**: retryable≠idempotent, 自动重试需retryable+idempotent(或idempotency_key), 无幂等保障只允许上层重新规划
- **Checkpoint/Resume立场**: 当前不支持通用resume, 中断后创建新run, SessionState/日志/事件/流输出不可作恢复源
- **事件投递语义**: 尽力而为(允许重复/丢失), EventEnvelope带event_id, 订阅方必须幂等, 事件顺序不作为业务真相源
- **重试版本链**: IterationAttempt(attempt_id+parent_attempt_id链), TransactionGroupAttempt同理, retry不覆盖原记录, 审计保留全链
- **Orchestrator编排**: OrchestratorAgent(编排感知prompt+spawn默认允许), 硬退出守卫(should_stop: spawn后3轮无新spawn强制停止), parent_run_id绑定实际run_id(非agent_id)
- **动态能力注入**: <agent-capabilities>XML块注入can_spawn/parallel_tool_calls/max_iterations/current_iteration/spawned_subagents, 运行时实值非硬编码
- **Skill系统(SKILL.md)**: 文件发现(skills/+~/.agent/skills/), YAML frontmatter, ${SKILL_DIR}变量, !`shell`预处理, $ARGUMENTS替换, invoke_skill工具, 渐进式披露(描述在context/body按需加载)
- **提示词XML结构**: <system-identity>/<runtime-environment>/<agent-capabilities>/<available-skills>/<active-skill>/<saved-memories>分区, LLM可明确区分各区域
- **冻结前缀(§14.8)**: FrozenPromptPrefix(prefix_hash+epoch), PromptPrefixManager缓存+轮换, 压缩器不碰前缀, 仅suffix可裁剪
- **会话模式双轨**: STATELESS(默认,全量发送,兼容所有provider) / STATEFUL(首轮全量+后续delta,省token,需provider支持), 适配器通过supports_stateful_session()声明
- **压缩-会话互斥**: STATEFUL模式跳过上下文压缩(避免delta索引偏移), STATELESS模式正常压缩(sliding_window/tool_result_summary)
- **工具schema缓存**: 每run缓存一次export_schemas(), 非每iteration重算
- **工具拦截反馈**: CapabilityPolicy/hook拦截的工具返回ToolResult(success=False)给LLM, 非静默跳过

## Commands
```bash
pip install -e ".[dev]"           # Install
pytest tests/                     # Tests (678 passed)
python -m agent_framework.main    # Interactive (Mock, no API key)
python -m agent_framework.main --config config/deepseek.json  # Real model
python run_demo.py                # Demo
```

## File Conventions
- pydantic v2 BaseModel / pydantic-settings BaseSettings
- TYPE_CHECKING for forward refs, runtime_checkable Protocols
- @tool decorator auto-detects async

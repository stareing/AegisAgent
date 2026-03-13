# 并行 Explore Agent 可行性与实施计划

## 1. 目标

在 `python -m agent_framework.main --config ...` 的交互界面中，实现类似：

- `Running N Explore agents...`
- 每个子任务独立进度、状态、token、耗时展示
- 并行执行后的统一汇总
- 可中断、可回看、可测试的稳定流程

## 2. 可行性结论

- 可行性：高
- 当前基础：子代理调度、配额、超时、委派链路、轨迹输出已具备
- 主要差距：编排层与实时事件流展示层缺失
- 人力估算：1人全职 2-4 周可交付可用版，4-6 周可稳定上线

## 3. 现状评估

### 已具备能力

1. 子代理执行基础完善
- `SubAgentRuntime` + `SubAgentScheduler`
- 支持并发、配额、超时、取消

2. 委派链路完整
- `spawn_agent` -> `ToolExecutor` -> `DelegationExecutor` -> `SubAgentRuntime`

3. 轨迹与日志基础具备
- CLI 已可展示 iteration/tool 轨迹

4. 测试基础存在
- 已有架构守护与故障注入相关测试文件

### 核心缺口

1. 缺 Explore 编排器
- 没有统一的“任务分解 -> 并行派发 -> 汇总”标准流程

2. 缺实时状态流 UI
- 目前主要是 run 完成后输出，非运行中动态刷新

3. 缺 worker 粒度展示
- 每个子代理的状态、token、错误、重试信息未形成标准前台视图

4. 缺交互控制能力
- 无 `/btw` 侧问、单 worker 取消/重试、展开折叠等

5. 缺 anti-bypass 闭环
- 规则扫描与 CI gate、故障注入未形成统一强约束流程

## 4. 分阶段实施计划

## Phase 1：并行编排 MVP（3-5 天）

目标：先跑通“并行执行+统一汇总”主链路。

交付：

1. 新增 `ExploreOrchestrator`
- 输入主任务
- 生成 `ExploreTaskSpec[]`
- 按 `max_workers` 并行 spawn
- 收集结果并汇总

2. 统一 worker 结果模型
- `worker_id`
- `status`（RUNNING/SUCCESS/FAILED/TIMEOUT/CANCELLED）
- `summary`
- `usage`
- `duration_ms`
- `error`

3. 最小可用命令入口
- 新增 `/explore <task>` 命令（或等价命令）

验收标准：
- 能一次并行跑 2-4 个 worker
- 能得到可读汇总输出

## Phase 2：实时事件流与终端动态展示（4-6 天）

目标：达到“运行中可见”的交互体验。

交付：

1. 事件协议
- `worker.started`
- `worker.progress`
- `worker.completed`
- `worker.failed`
- `orchestrator.summary`

2. CLI 实时渲染
- 显示总任务状态行
- 显示每个 worker 的当前状态、耗时、token
- 支持完成后展开详细结果

3. 历史回放
- 对单次 explore 的事件序列可回看

验收标准：
- 运行中动态刷新，而非仅结束后输出

## Phase 3：架构守护与故障注入补齐（3-5 天）

目标：保证复杂并行路径稳定可控。

交付：

1. anti-bypass 规则落地
- 把扫描发现项固化为 guard 测试
- 关键边界加禁止性断言

2. fault injection 套件
- 模型失败
- 工具失败
- 子代理超时
- 记忆层失败

3. CI Gate
- 守护测试失败时阻断合并

验收标准：
- 故障路径都有明确降级行为与测试覆盖

## Phase 4：交互增强（3-5 天）

目标：提升生产可用性与操控性。

交付：

1. 侧问通道（`/btw`）
- 不中断当前 explore 主流程

2. worker 控制能力
- 取消单 worker
- 重试失败 worker
- 展开/折叠 worker 明细

3. 汇总策略可配置
- strict merge / heuristic merge

验收标准：
- 用户可在运行中控制任务，而不必整体重跑

## Phase 5：性能与上线准备（2-4 天）

目标：控制成本并确保上线质量。

交付：

1. token 成本治理
- 长工具参数截断
- 大输出摘要化
- 上下文预算上限策略

2. 压测与回归
- 并发 worker 压测
- 长任务稳定性
- 资源泄漏检查

3. 文档与运维手册
- 配置说明
- 常见故障排查
- 日志字段说明

验收标准：
- 满足性能阈值与稳定性要求

## 5. 里程碑建议

1. M1（第 1 周）：Phase 1 完成（可并行执行）
2. M2（第 2 周）：Phase 2 完成（实时显示）
3. M3（第 3 周）：Phase 3 完成（守护与故障注入）
4. M4（第 4 周）：Phase 4/5 完成（交互增强与上线准备）

## 6. 风险与应对

1. 并发导致 token 成本失控
- 应对：worker token budget + 输出摘要 + 汇总限长

2. 事件流与状态一致性问题
- 应对：定义单一真相源（runtime state），事件仅观察

3. 子代理故障放大主流程失败
- 应对：失败隔离 + 局部重试 + 汇总降级

4. 规则绕过
- 应对：守护测试 + CI gate + 代码审查清单

## 7. 下一步执行建议

1. 优先实现 Phase 1 + Phase 2，尽快拿到可感知交互成果
2. 紧接 Phase 3，防止并发路径在真实场景中失稳
3. 以 PR 小步推进，每阶段拆成 2-4 个可回滚提交

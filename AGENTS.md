# AGENTS.md

本文件定义本仓库协作开发规则，基于当前代码实现与《架构开发文档 v2.3》的一致性审查结果。
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
* **记忆进度**：热跟新AGENTS.md
* **代码审核**：将由cladue code排查代码是否和需求一致

## 1. 使用目标

- 以 `架构开发文档.md` 为架构基线。
- 以“完成度 + 一致性”双指标驱动开发节奏。
- 先修边界与数据流，再补功能体验与性能优化。

## Project Structure

```
agent_framework/
├── agent/           # Agent loop, coordinator, state, skills
├── graph/           # Compiled graph engine (StateGraph, CompiledGraph)
├── tools/           # Tool decorator, registry, executor, delegation
│   ├── builtin/     # Built-in tools (8 categories)
│   ├── schemas/     # Parameter models & ToolCategory constants
│   └── shell/       # BashSession, ShellSessionManager (isolated)
├── memory/          # Saved memory manager, SQLite store
├── context/         # Context engineering, compression, 5-slot builder
├── subagent/        # Sub-agent factory, scheduler, runtime
├── hooks/           # Hook registry, executor, builtin hooks
├── plugins/         # Plugin manifest, loader, lifecycle, permissions
├── models/          # Pydantic v2 data models (incl. hook & plugin)
├── protocols/       # MCP client, A2A client
├── adapters/model/  # LLM adapters (11 providers)
├── infra/           # Config, logging, event bus, tracing
├── entry.py         # Framework facade
├── cli.py           # CLI entry point
└── main.py          # Interactive terminal
config/              # Model configuration files (JSON)
tests/               # 1120 tests across 29 files
```

---

## 2. 当前完成度快照（2026-03-13）

- 基础设施层（Config/Logger/EventBus/DiskStore）：`已实现`。
- 模型层（models/*）：`已实现`。
- Agent 主流程（Coordinator + Loop + ToolExecutor）：`可运行`。
- 记忆层（Base/Default/SQLite）：`可运行`。
- 上下文层（Source/Builder/Engineer）：`可运行`。
- 子 Agent（Factory/Scheduler/Runtime）：`部分完成`（存在权限边界缺口）。
- 协议层（MCP/A2A）：`部分完成`（实现存在接线缺口）。
- 测试：`基础集成测试已覆盖`，边界与协议关键路径覆盖不足。

## 3. 当前一致性结论（必须知晓）

以下问题在修复前，视为“架构未达标”：

1. 子 Agent 权限与递归防护链路存在绕过风险（Critical）。
2. Skill override 对真实模型调用参数未完全生效（Critical）。
3. A2A 路由已注册但接线不完整，默认不可用（High）。
4. CapabilityPolicy 未进入真实执行链（High）。
5. ContextCompressor 未纳入主流程（Medium）。
6. 运行异常路径清理不完整（Medium）。

## 4. 开发优先级（固定顺序）

1. 安全边界：子 Agent 最小权限、不可递归、能力上界必须真实生效。
2. 运行一致性：Skill 生命周期、effective config、生效边界可验证。
3. 协议可用性：MCP/A2A 接线必须端到端可执行。
4. 上下文质量：压缩策略接入、统计与行为一致。
5. 扩展体验：示例、CLI、文档补强。

## 5. 强制约束（实施中必须满足）

- 不允许“文档写了但调用链未接通”。
- 不允许通过共享对象破坏隔离（尤其是 ToolExecutor / Memory / Skill 状态）。
- 不允许让下层机制突破上层权限约束。
- 高风险工具必须显式确认或有明确拒绝策略。
- 子 Agent 返回给 LLM 仅允许摘要化结果，不返回完整内部 trace。

## 6. 测试门槛（合并前）

- 任何边界修复必须附带回归测试。
- 涉及下列模块的改动，必须新增/更新集成测试：
  - CapabilityPolicy + ScopedToolRegistry + hook 联合生效
  - Skill override 的生效与 run 结束失效
  - SubAgent 递归防护、配额、权限隔离
  - A2A/MCP 注册、路由与执行
- 若因环境无法执行测试，必须在交付说明中写明未验证项与风险。

## 7. 变更与提交要求

- 单次提交只做一个目标（如“修复子 Agent 权限链路”）。
- 提交说明必须包含：修复点、影响范围、验证方式。
- 禁止提交临时调试产物（如日志、缓存、`__pycache__`、临时脚本）。

## 8. 交付给用户的输出格式

- 先结论（是否达标），再问题清单（按严重度），最后给改动与验证。
- 审查输出必须包含具体文件与行号。
- 对未完成项必须给出：原因、阻塞条件、下一步。

## 9. Definition of Done（本项目）

满足以下条件才可标记“与架构文档一致”：

1. 权限优先级链真实生效并有测试证明。
2. Skill override 生命周期符合“仅当前 run”规则并有测试证明。
3. SubAgent 默认最小权限且不可递归绕过。
4. A2A/MCP 至少各有一条端到端通过路径。
5. 关键数据流（run -> iteration -> tool -> session -> memory）与文档一致。

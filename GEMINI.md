# Agent Framework - Project Context

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
* **记忆进度**：热跟新GEMINI.md
* **代码审核**：将由codex、claude 排查代码是否和需求一致
* 
## Project Overview
**Agent Framework** is an offline-first, highly extensible AI Agent runtime built with Python 3.11+. It is designed to be a robust foundation for building autonomous agents that can interact with various LLM providers, use local and remote tools (via MCP), and coordinate with other agents (via A2A).

### Key Features:
- **Layered Architecture:** Follows a strict Protocol/Base/Default pattern for all extensible modules.
- **Multi-Model Support:** Built-in adapters for LiteLLM, OpenAI, Anthropic, and Google GenAI.
- **Offline-First Memory:** Persistent memory storage using SQLite with automatic extraction and merging rules.
- **Extensible Tooling:** Supports local Python tools (@tool decorator), Model Context Protocol (MCP) servers, and Agent-to-Agent (A2A) delegation.
- **Sub-Agent Runtime:** Capabilities for spawning child agents with isolated or inherited memory scopes and resource quotas.
- **Context Engineering:** Advanced context management with multi-slot building (System -> Skills -> Memories -> History -> Input) and compression.
- **ReAct & Default Agents:** Built-in support for standard completion loops and ReAct (Reason+Act) strategies.

### Architecture Layers (Bottom to Top):
1.  **Infrastructure:** Config management, structured logging, event bus, and disk storage.
2.  **Models:** Pydantic v2 models for messages, tools, agents, sessions, and memory.
3.  **Adapters:** Unified interface for different LLM providers.
4.  **Protocols:** Standardized interfaces for MCP and A2A communication.
5.  **Memory:** SQLite-backed long-term memory management.
6.  **Context:** Logic for assembling, compressing, and engineering LLM prompts.
7.  **Tools:** Discovery, registration, and execution of local and remote capabilities.
8.  **Agent:** Core runtime, agent loops, coordinators, and skill routing.
9.  **Entry/CLI:** High-level facade and REPL interface for interacting with the framework.

---

## Building and Running

### Prerequisites
- Python 3.11 or higher.
- (Optional) API Keys for OpenAI, Anthropic, or Google GenAI (can be set via environment variables).

### Installation
```bash
# Clone the repository and install in editable mode with dev dependencies
pip install -e ".[all,dev]"
```

### Running the Demo
A comprehensive demo script is provided to showcase various framework features:
```bash
python run_demo.py
```

### Using the CLI
The framework includes a REPL CLI for direct interaction:
```bash
# Start the agent CLI (requires configuration or environment variables)
agent-cli
```

### Running Tests
The project uses `pytest` for unit and integration testing:
```bash
pytest tests/
```

---

## Development Conventions

### Coding Style
- **Type Safety:** Use Python type hints for all public interfaces and internal logic.
- **Pydantic v2:** All data models must inherit from `pydantic.BaseModel`.
- **Async First:** I/O bound operations (LLM calls, tool execution, MCP/A2A) should be `async`.
- **Structured Logging:** Use `structlog` for all logging; avoid standard `print()` in framework code.

### Architectural Rules
- **Layer Integrity:** Higher layers can call lower layers, but lower layers should not depend on higher ones. Use Protocols for abstraction.
- **Surgical Changes:** Only modify what is necessary. Adhere to existing patterns (e.g., the Protocol/Base/Default pattern).
- **Configuration:** Use `FrameworkConfig` (pydantic-settings) for all tunable parameters. Never hardcode magic values.

### Testing Practices
- **Integration Tests:** New features should be accompanied by integration tests in `tests/test_integration.py` using `MockModelAdapter`.
- **Regression Testing:** Always run existing tests before submitting changes to ensure no regressions are introduced.

### Tool Development
- Use the `@tool` decorator for local functions.
- Ensure tools are registered in the `GlobalToolCatalog` to be discoverable by agents.
- Categorize tools (e.g., "system", "network") for fine-grained permission control via `CapabilityPolicy`.

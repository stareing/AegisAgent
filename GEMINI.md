# Agent Framework - Project Context

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

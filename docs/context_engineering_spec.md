# Context Engineering Protocol Specification

> Status: Draft
> Scope: Defines the normative protocol for pluggable context engineering in this framework.
> Authority: This document defines what is compliant. The alignment plan defines how compliance is reached.

---

## 1. Purpose

This specification defines the standard protocol for context engineering components.

Its purpose is to prevent three recurring failure modes:

1. Hardcoded context construction with no extension path
2. Compression strategy locked to a single algorithm with no config override
3. Custom formatters requiring modification of entry.py or core classes

---

## 2. Terminology

### 2.1 Components

- `ContextEngineer`: Top-level orchestrator — receives agent state, produces LLM-ready messages
- `ContextBuilder`: Token budget manager — assembles context from slots, trims to fit
- `ContextCompressor`: History reduction — compresses old messages to reclaim token budget
- `ContextSourceProvider`: Format layer — collects and formats system prompt, memories, skills, session history

### 2.2 Interfaces

- `Protocol`: A `typing.Protocol` with `@runtime_checkable` — the normative contract
- `Strategy`: A named algorithm variant selectable via config (e.g., compression strategy)
- `Plugin`: An external module discovered at runtime via directory convention

### 2.3 Policies

- `ContextPolicy`: Controls whether compression is allowed — interpreted ONLY by ContextEngineer
- `MemoryPolicy`: Controls memory inclusion — interpreted ONLY by MemoryManager
- Policy interpretation uniqueness: each policy has exactly one interpreter

---

## 3. Protocol Definitions

### 3.1 ContextEngineerProtocol (exists, normative)

```python
@runtime_checkable
class ContextEngineerProtocol(Protocol):
    async def prepare_context_for_llm(
        self,
        agent_state: AgentState,
        context_materials: ContextMaterials,
    ) -> list[Message]: ...

    def set_skill_context(self, skill_prompt: str | None) -> None: ...

    def build_spawn_seed(
        self, session_state: SessionState, max_tokens: int,
    ) -> list[Message]: ...

    def apply_context_policy(self, policy: ContextPolicy) -> None: ...

    def report_context_stats(self) -> ContextStats: ...
```

### 3.2 ContextBuilderProtocol (NEW, required)

```python
@runtime_checkable
class ContextBuilderProtocol(Protocol):
    def calculate_tokens(self, messages: list[Message]) -> int: ...

    def set_token_budget(
        self, max_tokens: int, reserve_for_output: int,
    ) -> None: ...

    def build_context(
        self,
        system_messages: list[Message],
        session_groups: list[list[Message]],
        max_tokens: int,
    ) -> tuple[list[Message], dict]: ...

    def build_spawn_seed(
        self, session_state: SessionState, max_tokens: int,
    ) -> list[Message]: ...
```

### 3.3 ContextCompressorProtocol (NEW, required)

```python
@runtime_checkable
class ContextCompressorProtocol(Protocol):
    async def compress_if_needed(
        self,
        messages: list[Message],
        token_budget: int,
        model_adapter: Any,
    ) -> list[Message]: ...

    def reset(self) -> None: ...
```

### 3.4 ContextSourceProviderProtocol (NEW, required)

```python
@runtime_checkable
class ContextSourceProviderProtocol(Protocol):
    def collect_system_core(
        self,
        agent: Any,
        runtime_info: dict[str, str],
    ) -> list[Message]: ...

    def collect_saved_memory_block(
        self, memories: list[Any],
    ) -> list[Message]: ...

    def collect_session_groups(
        self, session_state: Any,
    ) -> list[list[Message]]: ...
```

---

## 4. Compression Strategies

### 4.1 Strategy Enum

```
SUMMARIZATION    — LLM-based incremental summarization (current default)
TRUNCATION       — Drop oldest messages, keep recent N groups
HYBRID           — Summarize old groups, keep recent N verbatim
NONE             — Never compress (fail if budget exceeded)
```

### 4.2 Strategy Selection

The active strategy MUST be selected via `FrameworkConfig.context.compression_strategy`.

The ContextCompressor MUST:
1. Accept a strategy parameter in its constructor or via config
2. Apply the selected strategy in `compress_if_needed()`
3. Fall back to SUMMARIZATION if strategy is not specified

### 4.3 Strategy Behavior

| Strategy | Old Messages | Recent Messages | Token Guarantee |
|----------|-------------|-----------------|-----------------|
| SUMMARIZATION | LLM summarizes | Kept verbatim | Soft (best-effort) |
| TRUNCATION | Dropped | Kept verbatim | Hard (always fits) |
| HYBRID | LLM summarizes | Kept verbatim (last N groups) | Soft |
| NONE | Kept verbatim | Kept verbatim | None (may overflow) |

### 4.4 Protected Groups

Regardless of strategy, the compressor MUST protect:
1. The system message (slot 0)
2. The last 2 transaction groups (most recent context)
3. Pinned messages (if any)

---

## 5. Plugin Discovery

### 5.1 Directory Convention

```
.context/
├── providers/
│   └── domain_provider.py      → custom ContextSourceProvider
├── compressors/
│   └── rag_compressor.py       → custom ContextCompressor
└── builders/
    └── sliding_window.py       → custom ContextBuilder
```

### 5.2 Discovery Rules

1. Framework scans `.context/` at project root during `setup()`
2. Each Python file MUST export a class matching the corresponding Protocol
3. Class name MUST end with the component type: `*Provider`, `*Compressor`, `*Builder`
4. If multiple files exist, the first alphabetically is used (or config specifies which)

### 5.3 Config Override

```json
{
  "context": {
    "source_provider_class": "my_module.CustomProvider",
    "compressor_class": "my_module.RAGCompressor",
    "builder_class": "my_module.SlidingWindowBuilder",
    "compression_strategy": "HYBRID"
  }
}
```

If `*_class` is set, it takes priority over directory discovery.

---

## 6. Lifecycle

### 6.1 Construction Order

```
1. ContextSourceProvider (stateless — created first)
2. ContextBuilder (budget-aware — needs config)
3. ContextCompressor (strategy-aware — needs config)
4. ContextEngineer (orchestrator — receives all three)
```

### 6.2 Per-Run Lifecycle

```
prepare_context_for_llm()
  → source_provider.collect_system_core()
  → source_provider.collect_saved_memory_block()
  → source_provider.collect_session_groups()
  → builder.build_context()           ← token budget enforcement
  → compressor.compress_if_needed()   ← if over budget
  → return messages
```

### 6.3 Cross-Run State

- `ContextCompressor`: MAY retain frozen summary across runs (SUMMARIZATION strategy)
- `ContextBuilder`: MUST NOT retain state across runs
- `ContextSourceProvider`: MUST NOT retain state across runs
- `ContextEngineer`: MAY retain prefix cache and skill context

---

## 7. Hook Integration

### 7.1 Hook Points

| Hook | Timing | Payload | Deniable |
|------|--------|---------|----------|
| `CONTEXT_PRE_BUILD` | Before build_context | task, memory_count, session_messages | YES |
| `CONTEXT_POST_BUILD` | After build_context | total_messages, total_tokens, groups_trimmed | NO |

### 7.2 Hook Semantics

- `CONTEXT_PRE_BUILD` DENY → skip context building for this iteration (use fallback)
- `CONTEXT_POST_BUILD` → advisory only (observe, log, metric)

---

## 8. Error Model

### 8.1 Token Budget Exceeded

If context exceeds budget after compression:

```python
class ContextBudgetExceeded(Exception):
    total_tokens: int
    budget_tokens: int
    strategy_used: str
```

The engineer MUST NOT silently truncate. It MUST either:
1. Compress further (if strategy allows)
2. Raise ContextBudgetExceeded

### 8.2 Compressor Failure

If the LLM summarization call fails:

```python
class CompressionError(Exception):
    strategy: str
    original_error: str
```

The engineer MUST fall back to TRUNCATION strategy, not fail the run.

---

## 9. Compliance Matrix

| ID | Requirement | Pass Condition |
|---|---|---|
| CE-001 | ContextBuilderProtocol defined | Protocol in protocols/core.py, runtime_checkable |
| CE-002 | ContextCompressorProtocol defined | Protocol in protocols/core.py, runtime_checkable |
| CE-003 | ContextSourceProviderProtocol defined | Protocol in protocols/core.py, runtime_checkable |
| CE-004 | Compression strategy configurable | config.context.compression_strategy controls behavior |
| CE-005 | Custom provider injectable | Entry accepts custom ContextSourceProvider |
| CE-006 | Custom compressor injectable | Entry accepts custom ContextCompressor |
| CE-007 | Custom builder injectable | Entry accepts custom ContextBuilder |
| CE-008 | Plugin discovery works | .context/ directory scanned at setup |
| CE-009 | Config class override works | context.*_class loads custom implementation |
| CE-010 | Protected groups respected | Last 2 groups + system never compressed |
| CE-011 | Hook PRE_BUILD fires | Hook called before context assembly |
| CE-012 | Hook POST_BUILD fires | Hook called after context assembly |
| CE-013 | Compressor fallback on error | LLM failure → TRUNCATION fallback |

---

## 10. Required Scenario Tests

### Scenario A: Custom Provider

```
1. Place custom_provider.py in .context/providers/
2. setup() discovers and uses it
3. prepare_context_for_llm() uses custom collect_system_core()
```

### Scenario B: Strategy Switch

```
1. Set config.context.compression_strategy = "TRUNCATION"
2. History exceeds budget
3. Oldest groups are dropped (not summarized)
4. Recent 2 groups preserved
```

### Scenario C: Config Class Override

```
1. Set config.context.compressor_class = "my_pkg.MyCompressor"
2. setup() loads MyCompressor via importlib
3. MyCompressor.compress_if_needed() is called during run
```

### Scenario D: Fallback

```
1. SUMMARIZATION strategy active
2. LLM call for summary fails
3. Compressor falls back to TRUNCATION
4. Run proceeds without failure
```

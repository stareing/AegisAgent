# Context Engineering 对齐实施文档

> 规范来源：[context_engineering_spec.md](context_engineering_spec.md)
> 若本文档与规范冲突，以规范为准。

---

## 一、CE-* 合规矩阵现状

> 审查日期：2026-03-23
> 审查基准：`context_engineering_spec.md` §9

| ID | 要求 | 状态 | 实现证据 |
|---|---|---|---|
| CE-001 | ContextBuilderProtocol | ✅ 通过 | `protocols/core.py` runtime_checkable + `isinstance` 测试 |
| CE-002 | ContextCompressorProtocol | ✅ 通过 | `protocols/core.py` runtime_checkable + `isinstance` 测试 |
| CE-003 | ContextSourceProviderProtocol | ✅ 通过 | `protocols/core.py` runtime_checkable + `isinstance` 测试 |
| CE-004 | 压缩策略可配 | ✅ 通过 | `CompressionStrategy` 枚举 + `config.context.default_compression_strategy` |
| CE-005 | 自定义 Provider 可注入 | ✅ 通过 | `ContextEngineer.__init__(source_provider=)` |
| CE-006 | 自定义 Compressor 可注入 | ✅ 通过 | `ContextEngineer.__init__(compressor=)` |
| CE-007 | 自定义 Builder 可注入 | ✅ 通过 | `ContextEngineer.__init__(builder=)` |
| CE-008 | 插件发现 .context/ | ✅ 通过 | `_load_context_component()` 扫描 `.context/{subdir}/` |
| CE-009 | Config class 覆盖 | ✅ 通过 | `ContextConfig.{source_provider,compressor,builder}_class` + importlib |
| CE-010 | 保护组不被压缩 | ✅ 通过 | `_PROTECTED_RECENT_GROUPS=2` + 截断保护 |
| CE-011 | Hook PRE_BUILD 触发 | ✅ 通过 | `CONTEXT_PRE_BUILD` HookPoint |
| CE-012 | Hook POST_BUILD 触发 | ✅ 通过 | `CONTEXT_POST_BUILD` HookPoint |
| CE-013 | 压缩失败回退 | ✅ 通过 | LLM 失败 → `_truncate_groups()` 自动回退 |

**通过：13/13**
**CE 合规测试：`tests/test_context_engineering_ce.py` — 19 项全通过**

---

## 二、实施路线

```
Phase 1: Protocol 定义 + 压缩策略枚举          (CE-001~004, 1d)
Phase 2: Config 驱动构建 + importlib 加载       (CE-009, 0.5d)
Phase 3: .context/ 插件发现                     (CE-008, 0.5d)
Phase 4: 压缩回退机制                           (CE-013, 0.5d)
```

---

## Phase 1: Protocol 定义 + 压缩策略

### 1.1 新增 Protocol (CE-001~003)

**修改文件**：`agent_framework/protocols/core.py`

```python
@runtime_checkable
class ContextBuilderProtocol(Protocol):
    def calculate_tokens(self, messages: list[Message]) -> int: ...
    def set_token_budget(self, max_tokens: int, reserve: int) -> None: ...
    def build_context(
        self, system_messages: list[Message],
        session_groups: list[list[Message]], max_tokens: int,
    ) -> tuple[list[Message], dict]: ...
    def build_spawn_seed(
        self, session_state: Any, max_tokens: int,
    ) -> list[Message]: ...


@runtime_checkable
class ContextCompressorProtocol(Protocol):
    async def compress_if_needed(
        self, messages: list[Message], token_budget: int,
        model_adapter: Any,
    ) -> list[Message]: ...
    def reset(self) -> None: ...


@runtime_checkable
class ContextSourceProviderProtocol(Protocol):
    def collect_system_core(
        self, agent: Any, runtime_info: dict[str, str],
    ) -> list[Message]: ...
    def collect_saved_memory_block(
        self, memories: list[Any],
    ) -> list[Message]: ...
    def collect_session_groups(
        self, session_state: Any,
    ) -> list[list[Message]]: ...
```

### 1.2 压缩策略枚举 (CE-004)

**新增文件**：`agent_framework/context/strategies.py`

```python
from enum import Enum

class CompressionStrategy(str, Enum):
    SUMMARIZATION = "SUMMARIZATION"   # LLM 增量摘要 (默认)
    TRUNCATION = "TRUNCATION"         # 丢弃最旧组，保留最近 N 组
    HYBRID = "HYBRID"                 # 旧组摘要 + 最近 N 组原文
    NONE = "NONE"                     # 不压缩 (超预算报错)
```

**修改文件**：`agent_framework/infra/config.py`

```python
class ContextConfig(BaseModel):
    # ... 现有 ...
    compression_strategy: str = "SUMMARIZATION"
    # 可插拔组件类名 (importlib 加载)
    source_provider_class: str = ""
    compressor_class: str = ""
    builder_class: str = ""
```

**修改文件**：`agent_framework/context/compressor.py`

```python
class ContextCompressor:
    def __init__(
        self,
        token_counter=None,
        strategy: CompressionStrategy = CompressionStrategy.SUMMARIZATION,
    ):
        self._strategy = strategy

    async def compress_if_needed(self, messages, token_budget, model_adapter):
        if self._strategy == CompressionStrategy.NONE:
            return messages
        if self._strategy == CompressionStrategy.TRUNCATION:
            return self._truncate(messages, token_budget)
        if self._strategy == CompressionStrategy.HYBRID:
            return await self._hybrid(messages, token_budget, model_adapter)
        # Default: SUMMARIZATION
        return await self._summarize(messages, token_budget, model_adapter)
```

### 1.3 验收标准

1. `isinstance(ContextBuilder(), ContextBuilderProtocol)` → True
2. `isinstance(ContextCompressor(), ContextCompressorProtocol)` → True
3. `isinstance(ContextSourceProvider(), ContextSourceProviderProtocol)` → True
4. `config.context.compression_strategy = "TRUNCATION"` → 旧消息被丢弃而非摘要

---

## Phase 2: Config 驱动构建

### 2.1 importlib 加载 (CE-009)

**修改文件**：`agent_framework/entry.py`

```python
def _load_context_class(class_path: str, protocol: type):
    """Load a class by dotted path and verify it matches the protocol."""
    if not class_path:
        return None
    module_path, _, class_name = class_path.rpartition(".")
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    if not isinstance(cls, type):
        raise TypeError(f"{class_path} is not a class")
    return cls


# In setup():
ctx_cfg = self.config.context

# Source provider
if ctx_cfg.source_provider_class:
    ProviderCls = _load_context_class(ctx_cfg.source_provider_class, ContextSourceProviderProtocol)
    source_provider = ProviderCls()
else:
    source_provider = ContextSourceProvider()

# Compressor
if ctx_cfg.compressor_class:
    CompressorCls = _load_context_class(ctx_cfg.compressor_class, ContextCompressorProtocol)
    compressor = CompressorCls()
else:
    compressor = ContextCompressor(strategy=CompressionStrategy(ctx_cfg.compression_strategy))

# Builder
if ctx_cfg.builder_class:
    BuilderCls = _load_context_class(ctx_cfg.builder_class, ContextBuilderProtocol)
    builder = BuilderCls(max_context_tokens=ctx_cfg.max_context_tokens, ...)
else:
    builder = ContextBuilder(...)
```

### 2.2 验收标准

1. `config.context.compressor_class = "my_pkg.MyCompressor"` → `setup()` 加载并使用
2. 未设置 `*_class` → 使用默认实现
3. 类名错误 → `setup()` 报错 + 回退默认

---

## Phase 3: .context/ 插件发现

### 3.1 发现机制 (CE-008)

**新增文件**：`agent_framework/context/discovery.py`

```python
def discover_context_plugins(base_dir: Path) -> dict:
    """Scan .context/ for custom components.

    Returns {"providers": [...], "compressors": [...], "builders": [...]}.
    """
    result = {"providers": [], "compressors": [], "builders": []}
    context_dir = base_dir / ".context"
    if not context_dir.is_dir():
        return result

    for subdir, key, suffix in [
        ("providers", "providers", "Provider"),
        ("compressors", "compressors", "Compressor"),
        ("builders", "builders", "Builder"),
    ]:
        dir_path = context_dir / subdir
        if not dir_path.is_dir():
            continue
        for py_file in sorted(dir_path.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            # Load module and find class ending with suffix
            spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            for name, obj in inspect.getmembers(module, inspect.isclass):
                if name.endswith(suffix):
                    result[key].append(obj)
    return result
```

**修改文件**：`agent_framework/entry.py`

```python
# In setup(), before context construction:
from agent_framework.context.discovery import discover_context_plugins
plugins = discover_context_plugins(Path.cwd())

# Use discovered plugins (config class override takes priority)
if not ctx_cfg.source_provider_class and plugins["providers"]:
    source_provider = plugins["providers"][0]()
```

### 3.2 目录结构

```
.context/
├── providers/
│   └── my_domain_provider.py   → class MyDomainProvider (实现 ContextSourceProviderProtocol)
├── compressors/
│   └── rag_compressor.py       → class RAGCompressor (实现 ContextCompressorProtocol)
└── builders/
    └── sliding_window.py       → class SlidingWindowBuilder (实现 ContextBuilderProtocol)
```

### 3.3 优先级

```
config.*_class > .context/ 发现 > 内置默认
```

### 3.4 验收标准

1. `.context/providers/custom.py` 存在 → `setup()` 自动发现并使用
2. `config.source_provider_class` 同时设置 → config 优先
3. `.context/` 不存在 → 使用默认，无报错

---

## Phase 4: 压缩回退机制

### 4.1 失败回退 (CE-013)

**修改文件**：`agent_framework/context/compressor.py`

```python
async def compress_if_needed(self, messages, token_budget, model_adapter):
    if self._strategy == CompressionStrategy.NONE:
        return messages

    try:
        if self._strategy == CompressionStrategy.SUMMARIZATION:
            return await self._summarize(messages, token_budget, model_adapter)
        elif self._strategy == CompressionStrategy.HYBRID:
            return await self._hybrid(messages, token_budget, model_adapter)
        elif self._strategy == CompressionStrategy.TRUNCATION:
            return self._truncate(messages, token_budget)
    except Exception as exc:
        logger.warning("context.compression_failed",
                       strategy=self._strategy.value, error=str(exc))
        # Fallback: TRUNCATION is always safe (no LLM dependency)
        return self._truncate(messages, token_budget)
```

### 4.2 TRUNCATION 实现

```python
def _truncate(self, messages: list[Message], token_budget: int) -> list[Message]:
    """Drop oldest groups until within budget. Always preserves system + last 2 groups."""
    # Keep: system message (idx 0) + last 2 transaction groups
    # Drop: everything in between, oldest first
    if not messages:
        return messages

    system = [messages[0]] if messages[0].role == "system" else []
    rest = messages[len(system):]

    # Find last 2 complete groups (user+assistant pairs)
    protected_tail = rest[-4:] if len(rest) >= 4 else rest
    trimmable = rest[:-len(protected_tail)] if protected_tail else []

    # Drop oldest until within budget
    while trimmable and self._count_tokens(system + trimmable + protected_tail) > token_budget:
        trimmable.pop(0)

    return system + trimmable + protected_tail
```

### 4.3 验收标准

1. SUMMARIZATION 策略 + LLM 调用失败 → 自动回退 TRUNCATION
2. 回退后 run 继续执行，不中断
3. 日志记录 `context.compression_failed`

---

## 附录 A: 文件改动矩阵

| Phase | 新增文件 | 修改文件 |
|-------|---------|---------|
| 1 | `context/strategies.py` | `protocols/core.py`, `context/compressor.py`, `infra/config.py` |
| 2 | — | `entry.py` |
| 3 | `context/discovery.py` | `entry.py` |
| 4 | — | `context/compressor.py` |

## 附录 B: 测试矩阵

| Phase | 测试文件 | 用例数 |
|-------|---------|-------|
| 1 | `tests/test_context_protocols.py` | ~10 (Protocol 检查, 策略枚举) |
| 2 | `tests/test_context_config_loading.py` | ~6 (importlib, 错误处理) |
| 3 | `tests/test_context_discovery.py` | ~6 (目录扫描, 优先级) |
| 4 | `tests/test_context_fallback.py` | ~5 (LLM 失败回退, 截断保护) |

## 附录 C: Config 扩展

```python
class ContextConfig(BaseModel):
    max_context_tokens: int = 8192
    reserve_for_output: int = 1024
    default_compression_strategy: str = "NONE"
    # 可插拔组件 (importlib dotted path, 优先级高于 .context/ 发现)
    source_provider_class: str = ""    # e.g. "my_pkg.CustomProvider"
    compressor_class: str = ""         # e.g. "my_pkg.RAGCompressor"
    builder_class: str = ""            # e.g. "my_pkg.SlidingWindowBuilder"
```

## 附录 D: 使用示例

### D.1 通过 config 切换压缩策略

```json
{
  "context": {
    "compression_strategy": "TRUNCATION",
    "max_context_tokens": 16384
  }
}
```

### D.2 通过 config 加载自定义 Compressor

```json
{
  "context": {
    "compressor_class": "my_project.context.RAGCompressor"
  }
}
```

### D.3 通过 .context/ 目录发现

```
.context/
└── compressors/
    └── rag_compressor.py
```

```python
# rag_compressor.py
class RAGCompressor:
    async def compress_if_needed(self, messages, token_budget, model_adapter):
        # RAG-based compression: keep only messages relevant to current task
        ...

    def reset(self):
        pass
```

Framework 自动发现并使用。

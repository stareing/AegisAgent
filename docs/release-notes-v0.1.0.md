## AegisAgent v0.1.0 — Full SDK

**49 files changed, 9925 insertions | 2028 tests passed | Zero regression**

### Highlights

- **Full SDK** — 89 public methods covering 100% of framework capabilities
- **Gemini CLI Tools** — 4-stage edit, git grep, rate-limited fetch, batch file read
- **Prompt Engineering** — Modular snippets, security mandates, context efficiency rules
- **KV Cache Optimized** — Immutable system prompt, prefix-stable across iterations
- **Command System** — Typed protocol with /init, /restore, /memory, /plugins, /model
- **Dual Distribution** — `pip install agent-framework` or standalone SDK

---

### SDK (89 methods + 3 properties)

```python
from agent_framework.sdk import AgentSDK, SDKConfig

async with AgentSDK(SDKConfig(model_adapter_type="anthropic", api_key="sk-...")) as sdk:
    result = await sdk.run("Build a REST API")

    # Streaming
    async for event in sdk.run_stream("Analyze code"):
        print(event.data.get("text", ""), end="")

    # Parallel isolated execution
    results = await sdk.run_parallel(["Task A", "Task B", "Task C"])

    # Graph engine (LangGraph-compatible)
    graph = sdk.create_graph(State)
    app = sdk.compile_graph(graph)
    result = await sdk.invoke_graph(app, initial_state)
```

**Full coverage**: execution, streaming, JSONL, tools, skills, memory, hooks, plugins, MCP, A2A, graph, team, checkpoints, sandbox, IDE server, policy engine, approval modes, multi-instance isolation, event callbacks, health check.

---

### Tool Upgrades (Ported from Gemini CLI)

| Tool | Before | After |
|---|---|---|
| **edit_file** | Exact match only | 4-stage cascade (exact → flexible → regex → fuzzy) + diff preview |
| **read_file** | Full read only | `start_line`/`end_line` range + `cat -n` format + pagination |
| **write_file** | Direct write | CRLF/LF preservation + unified diff + stats |
| **grep_search** | Python walk only | `git grep` first (10x faster) + `names_only` + `exclude_pattern` |
| **web_fetch** | Basic HTTP | Rate limiting (10/min) + LRU cache (15min) + GitHub URL conversion + 250KB |
| **New: read_many_files** | — | Batch read with water-filling budget allocation |
| **New: ask_user** | — | Interactive question with options |

---

### Prompt Engineering

**Modular shared snippets** (single definition, multi-prompt reuse):
- `SECURITY_MANDATES` — Credential protection, source control safety
- `CONTEXT_EFFICIENCY_RULES` — Per-tool XML guidelines (read_file start_line, grep over bash)
- `GIT_RULES` — Prefer new commits, never force-push main, never skip hooks
- `ENGINEERING_STANDARDS` — Follow conventions, verify libraries, test

**New prompts**: `PLAN_MODE_ADDON`, `CONTEXT_COMPRESSION_PROMPT_EN`

---

### KV Cache Architecture

System prompt is **immutable** — only rotates when tools/MCP/skills hash changes:

```
[system (IMMUTABLE)] [session history (STABLE)] ... [injection (LAST)]
 |--- KV cached -----------------------------|      ^ only this changes
```

- Dynamic values (iteration count, todo) removed from context pipeline
- Compression triggers one-time cache miss, then frozen summary reuses across iterations
- Typical cache reuse: **95%+** after warm-up

---

### Command System

Typed protocol with hierarchical subcommands:

| Command | Description |
|---|---|
| `/init` | Analyze project, generate CLAUDE.md |
| `/restore [id]` | Git checkpoint restoration + conversation replay |
| `/memory show\|add\|reload\|list\|clear\|pin\|unpin` | Memory management |
| `/plugins list\|enable\|disable\|info` | Plugin lifecycle |
| `/model [name]\|list` | Runtime model switching |

---

### Security & Safety

- **ApprovalMode** — `DEFAULT` / `AUTO_EDIT` / `PLAN` tri-mode switching
- **Risk Scorer** — 5-tier command risk assessment (SAFE → CRITICAL)
- **Multi-level Sandbox** — Auto-selects none/native/container/strict per command risk
- **Declarative Policy Engine** — TOML rules with wildcards, pattern matching, approval memory
- **MCP IDE Server** — 7 tools + 3 resources + JSON-RPC stdio for VS Code integration

---

### Installation

```bash
# Full framework
pip install agent-framework

# With specific provider
pip install agent-framework[anthropic]
pip install agent-framework[openai]
pip install agent-framework[google]
pip install agent-framework[all]

# SDK only
pip install agent-framework-sdk
```

---

### CI/CD

- GitHub Actions CI: Python 3.11/3.12/3.13 matrix
- Automated release: `git tag v0.1.0 && git push origin v0.1.0`
- PyPI publish via Trusted Publisher (OIDC, no token needed)

---

### Stats

- **49 files changed** across 14 commits
- **9,925 lines added** (net)
- **2,028 tests** passing
- **89 SDK methods** + 22 public types
- **0 regressions**

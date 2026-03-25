# SDK 构建与发行指南

## 分发模式

| 模式 | 包名 | 安装命令 | 说明 |
|---|---|---|---|
| **主包（推荐）** | `agent-framework` | `pip install agent-framework` | 包含 SDK + 完整框架 |
| **独立 SDK** | `agent-framework-sdk` | `pip install agent-framework-sdk` | 轻量包装，依赖主包 |

## 快速使用

```bash
# 安装（含 SDK）
pip install -e ".[dev]"

# 使用
from agent_framework.sdk import AgentSDK, SDKConfig
```

---

## 本地构建

```bash
# 安装构建工具
pip install build twine

# 构建主包
python -m build
ls dist/
# → agent_framework-0.1.0-py3-none-any.whl
# → agent_framework-0.1.0.tar.gz

# 构建独立 SDK 包
cd sdk && python -m build
ls dist/
# → agent_framework_sdk-0.1.0-py3-none-any.whl
# → agent_framework_sdk-0.1.0.tar.gz

# 验证包元数据
twine check dist/*
twine check sdk/dist/*

# 本地安装测试
pip install dist/agent_framework-0.1.0-py3-none-any.whl
```

## Provider 可选依赖

```bash
pip install agent-framework[anthropic]   # Claude
pip install agent-framework[openai]      # GPT
pip install agent-framework[google]      # Gemini
pip install agent-framework[mcp]         # MCP 协议
pip install agent-framework[a2a]         # A2A 协议
pip install agent-framework[otel]        # OpenTelemetry
pip install agent-framework[all]         # 全部 provider
```

---

## GitHub 自动发行

### 触发方式

```bash
# 打 tag → 推送 → 自动触发 Release workflow
git tag v0.1.0
git push origin v0.1.0
```

### 自动化流水线

```
git push tag v0.1.0
        │
        ▼
┌─ test ──────────────────────────────┐
│  Python 3.11 / 3.12 / 3.13 矩阵     │
│  pytest 2028+ tests                  │
│  SDK 完整性检查 (≥89 methods)         │
└──────────────┬──────────────────────┘
               │ ✅ 通过
               ▼
┌─ build ─────────────────────────────┐
│  python -m build                     │
│  → agent_framework-x.y.z.whl        │
│  → agent_framework-x.y.z.tar.gz     │
│                                      │
│  cd sdk/ && python -m build          │
│  → agent_framework_sdk-x.y.z.whl    │
│  → agent_framework_sdk-x.y.z.tar.gz │
│                                      │
│  twine check (验证包元数据)            │
└──────────────┬──────────────────────┘
               │
        ┌──────┴──────┐
        ▼             ▼
┌─ GitHub Release ┐ ┌─ PyPI Publish ────────┐
│  自动生成变更日志 │ │  Trusted Publisher    │
│  附加 .whl 下载  │ │  (OIDC, 无需 token)   │
│  prerelease 标记 │ │                       │
│  (alpha/beta/rc) │ │  pip install agent-   │
└─────────────────┘ │    framework==x.y.z   │
                    └───────────────────────┘
```

### Workflow 文件

- `.github/workflows/ci.yml` — 每次 push/PR 自动测试
- `.github/workflows/release.yml` — tag 触发构建 + 发布

---

## PyPI Trusted Publisher 配置（一次性）

### 步骤 1: PyPI 端

1. 注册 [pypi.org](https://pypi.org) 账号
2. 创建项目 `agent-framework`
3. 进入 Settings → Publishing → Add a new publisher
4. 填写：
   - Owner: `stareing`
   - Repository: `AegisAgent`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

5. 对 `agent-framework-sdk` 重复以上步骤

### 步骤 2: GitHub 端

1. 进入 repo Settings → Environments
2. 创建 `pypi` environment
3. （可选）添加保护规则：Required reviewers

### 验证

推送 tag 后，Release workflow 会自动通过 OIDC 认证发布到 PyPI，无需 API token。

---

## 版本号规范

| Tag 格式 | 语义 | GitHub Release 标记 | PyPI |
|---|---|---|---|
| `v0.1.0` | 正式版 | Release | ✅ |
| `v0.2.0-alpha.1` | 内测版 | Pre-release | ✅ |
| `v0.2.0-beta.1` | 公测版 | Pre-release | ✅ |
| `v0.2.0-rc.1` | 候选版 | Pre-release | ✅ |

版本号需要在两处同步更新：
- `pyproject.toml` → `version = "0.1.0"`
- `sdk/pyproject.toml` → `version = "0.1.0"`

---

## 项目结构

```
my-agent/
├── .github/
│   └── workflows/
│       ├── ci.yml               ← CI: test on push/PR
│       └── release.yml          ← Release: build + GitHub Release + PyPI
├── pyproject.toml               ← 主包 agent-framework
├── agent_framework/
│   ├── sdk/                     ← SDK 子包 (89 methods + 3 props)
│   │   ├── __init__.py          ← 22 types 导出
│   │   ├── client.py            ← AgentSDK 门面
│   │   ├── config.py            ← SDKConfig 配置
│   │   └── types.py             ← 22 个公开类型
│   └── ...                      ← 框架核心模块
├── sdk/
│   ├── pyproject.toml           ← 独立 SDK 包 agent-framework-sdk
│   └── README.md                ← SDK 使用文档
├── tests/
│   └── test_gemini_features.py  ← SDK 测试 (90+ tests)
└── docs/
    └── sdk-build-and-release.md ← 本文档
```

---

## SDK API 概览 (89 方法)

| 能力域 | 方法 |
|---|---|
| **执行** | `run` `run_sync` `run_stream` `run_stream_jsonl` `run_isolated` `run_parallel` |
| **取消** | `create_cancel_token` |
| **命令** | `execute_command` |
| **工具** | `tool` `register_tool` `register_tools_from_module` `list_tools` `execute_tool` `export_tool_schemas` |
| **对话** | `begin_conversation` `end_conversation` `get_history` `export_history` `import_history` |
| **技能** | `register_skill` `list_skills` `remove_skill` `activate_skill` `deactivate_skill` `get_active_skill` |
| **记忆** | `list_memories` `forget_memory` `pin_memory` `unpin_memory` `activate_memory` `deactivate_memory` `clear_memories` `set_memory_enabled` |
| **Hook** | `register_hook` `unregister_hook` `list_hooks` `on_event` `off_event` |
| **插件** | `load_plugin` `enable_plugin` `disable_plugin` `list_plugins` `list_plugin_agent_templates` `get_plugin_agent_templates` |
| **MCP** | `setup_mcp` `list_mcp_resources` `read_mcp_resource` `list_mcp_prompts` `get_mcp_prompt` `list_mcp_resource_templates` |
| **A2A** | `setup_a2a` `build_a2a_server` |
| **Graph** | `create_graph` `compile_graph` `invoke_graph` `stream_graph` `create_agent_node` `create_tool_node` `create_memory_saver` |
| **模型** | `list_models` `resolve_model_id` |
| **团队** | `drain_team_notifications` `peek_team_notifications` `has_pending_team_notifications` `mark_team_notifications_delivered` `drain_team_summaries` `setup_run_dispatcher` |
| **沙箱** | `assess_command_risk` `select_sandbox` |
| **IDE** | `create_ide_server` |
| **检查点** | `save_checkpoint` `list_checkpoints` `restore_checkpoint` |
| **上下文** | `get_context_stats` `compact_history` |
| **策略** | `add_policy_rule` `list_policy_rules` `clear_policy_rules` |
| **模式** | `set_approval_mode` |
| **隔离** | `create_isolated` `fork` |
| **身份** | `resolve_identity` |
| **工作区** | `init_workspace` `list_workspace_templates` |
| **诊断** | `health_check` `get_info` |
| **生命周期** | `setup` `shutdown` `cleanup` `__aenter__` `__aexit__` |

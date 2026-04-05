# my-agent 安全 Sandbox 改造开发计划

## 1. 目标

把当前“策略级限制”升级为“强隔离执行”：

- 工具执行默认在受限沙箱内进行
- 主机文件系统、网络、环境变量最小暴露
- 子 Agent 与主 Agent 都受统一强制策略约束
- 可审计、可回放、可配额

## 2. 当前差距（基线）

- 已有：`CapabilityPolicy`、`ScopedToolRegistry`、运行时 hook 拦截
- 缺失：进程级/系统级隔离（当前 `run_command` 直接 `subprocess.run(shell=True)`）
- 风险点：`get_env` 默认无需确认

---

## 3. 分阶段实施

## Phase 0（1天）- 立即止血

1. 将高风险系统工具默认关闭（或仅开发模式开启）
2. `get_env` 改为 `require_confirm=True`，并加变量白名单
3. CLI/配置加入 `sandbox.required=true`（生产强制）

验收标准：

- 未启用 sandbox 时，系统工具不可执行

## Phase 1（3-5天）- Sandbox 执行器 MVP

1. 新增 `SandboxExecutor` 抽象（替换直接 `subprocess`）
2. 实现本地隔离后端（优先：`nsjail`/`bubblewrap`/Docker 三选一）
3. `ToolExecutor` 中 `system` 类工具全部路由到 `SandboxExecutor`
4. 支持基础限制：
- CPU/内存/超时
- 只读根文件系统
- 仅挂载工作目录白名单
- 默认禁网

验收标准：

- 同一命令在宿主和沙箱可区分（沙箱内无法访问主机敏感路径）

## Phase 2（3-4天）- 策略中心化

1. 新增 `SandboxPolicy`（按工具/agent/subagent分级）
2. 将 `CapabilityPolicy` 与 `SandboxPolicy` 联动（拒绝优先）
3. 增加网络策略：`deny_all` / 域名白名单 / 端口白名单
4. 增加文件策略：读写路径 allowlist、禁止越权路径

验收标准：

- 配置变更可实时影响工具执行权限，无需改代码

## Phase 3（2-3天）- 可观测与审计

1. 每次工具执行生成审计记录：
- tool、命令、参数摘要、exit code、耗时、策略命中
2. 结构化日志落盘 + trace id 关联 run/spawn
3. 增加“拒绝原因”标准错误码

验收标准：

- 能追踪一次 run 内所有沙箱行为与拦截原因

## Phase 4（3-4天）- 测试与攻防回归

1. 单元测试：策略匹配、路径逃逸、环境变量泄漏
2. 集成测试：subagent 下 system/network 默认拒绝
3. 对抗测试：
- `../../` 路径逃逸
- shell 注入
- fork bomb/超时
4. 基准测试：性能回归阈值（如执行开销 < 20%）

验收标准：

- 安全测试全过，原有集成测试不回退

---

## 4. 代码改造清单（建议）

1. 新增：
- `agent_framework/sandbox/`
- `executor.py`（接口）
- `policy.py`
- `backends/{bwrap,docker,nsjail}.py`
- `audit.py`

2. 修改：
- `agent_framework/tools/builtin/system.py`（不再直连 `subprocess`）
- `agent_framework/tools/executor.py`（system 路由 sandbox）
- `agent_framework/infra/config.py`（新增 sandbox 配置）
- `agent_framework/entry.py`（初始化 sandbox 组件）

3. 测试：
- `tests/test_sandbox_*.py`
- `tests/test_integration.py` 增补 sandbox 场景

---

## 5. 配置设计（MVP）

```yaml
sandbox:
  enabled: true
  required: true
  backend: bubblewrap
  network_mode: deny_all
  timeout_seconds: 10
  memory_mb: 256
  cpu_quota: 0.5
  writable_paths:
    - /tmp
  readable_paths:
    - /home/jiojio/my-agent
  env_allowlist:
    - PATH
    - LANG
```

---

## 6. 里程碑与交付

1. M1（本周）：Phase 0 + Phase 1，完成强制沙箱执行
2. M2（下周）：Phase 2 + Phase 3，完成策略中心和审计
3. M3（第三周）：Phase 4，完成安全回归与发布文档

---

## 7. 风险与决策点

1. 后端选型（Docker vs bwrap vs nsjail）影响部署复杂度
2. 跨平台一致性（Linux 优先，macOS/Windows 需降级策略）
3. 性能开销与安全强度平衡

---

## 8. 下一步建议

1. 先定后端选型（建议 Linux 首选 `bubblewrap`）
2. 我可以直接开始落地 Phase 0 + Phase 1 的代码骨架与首批测试

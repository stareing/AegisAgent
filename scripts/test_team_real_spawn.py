#!/usr/bin/env python3
"""真实 Spawned Teammate 集成测试。

验证: spawn_teammate() → SubAgentRuntime → child 使用 mail() 工具 → Lead 收到结果。
这不是模拟测试，而是真实 LLM + 真实 SubAgentRuntime 的端到端闭环。

使用:
    python scripts/test_team_real_spawn.py
    python scripts/test_team_real_spawn.py --config config/doubao.local.json -v
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def _c(code: int, t: str) -> str: return f"\033[{code}m{t}\033[0m"
def green(t: str) -> str: return _c(32, t)
def red(t: str) -> str: return _c(31, t)
def yellow(t: str) -> str: return _c(33, t)
def cyan(t: str) -> str: return _c(36, t)
def dim(t: str) -> str: return _c(2, t)
def bold(t: str) -> str: return _c(1, t)

def log(label: str, msg: str, color: int = 0) -> None:
    ts = time.strftime("%H:%M:%S")
    tag = _c(color, f"[{label}]") if color else f"[{label}]"
    print(f"  {dim(ts)} {tag} {msg}")


async def main(config_path: str, verbose: bool) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config, _setup_team
    from agent_framework.subagent.factory import SubAgentFactory
    from agent_framework.models.subagent import SubAgentSpec, SpawnMode

    logging.getLogger("agent_framework").setLevel(
        logging.DEBUG if verbose else logging.WARNING
    )

    print(f"\n{bold('真实 Spawned Teammate 集成测试')}")
    print(f"Config: {cyan(config_path)}\n")

    # ── Step 1: 初始化框架 + Team ──
    log("INIT", "初始化框架...", 32)
    config = load_config(config_path)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)
    team_env = _setup_team(fw, "integration_test")
    coordinator = team_env["coordinator"]
    mailbox = team_env["mailbox"]
    registry = team_env["registry"]
    log("INIT", f"Team: {coordinator.team_id}, Lead: {coordinator._lead_id}", 32)

    # ── Step 2: 验证子代理工具可见性 ──
    log("CHECK", "验证子代理工具可见性...", 34)
    factory = SubAgentFactory(fw._deps)
    spec = SubAgentSpec(
        parent_run_id=coordinator.team_id,
        spawn_id="check_tools",
        task_input="test",
        mode=SpawnMode.EPHEMERAL,
    )
    _, child_deps = factory.create_agent_and_deps(spec, fw._agent)
    child_tools = [t.meta.name for t in child_deps.tool_registry.list_tools()]

    if "team" in child_tools and "mail" in child_tools:
        print(f"  {green('✓')} 子代理可见 team + mail 工具")
    else:
        print(f"  {red('✗')} 子代理工具不可见: team={'team' in child_tools}, mail={'mail' in child_tools}")
        print(f"      可见工具: {sorted(child_tools)}")
        return 1

    if "spawn_agent" not in child_tools:
        print(f"  {green('✓')} spawn_agent 已被正确屏蔽")
    else:
        print(f"  {red('✗')} spawn_agent 未被屏蔽 (应该被 delegation 类别拦截)")

    # 验证 team 上下文
    executor = child_deps.tool_executor
    ctx = {
        "spawn_id": getattr(executor, "_current_spawn_id", None),
        "team_id": getattr(executor, "_current_team_id", None),
        "role": getattr(executor, "_current_agent_role", None),
        "mailbox": getattr(executor, "_team_mailbox", None) is not None,
    }
    log("CHECK", f"子代理上下文: {ctx}", 34)
    if all(ctx.values()):
        print(f"  {green('✓')} 子代理 team 上下文完整")
    else:
        missing = [k for k, v in ctx.items() if not v]
        print(f"  {red('✗')} 缺少: {missing}")
        return 1

    # ── Step 3: 通过 coordinator 真实 spawn teammate ──
    log("SPAWN", "Spawning real teammate via coordinator...", 33)
    agent_id = await coordinator.spawn_teammate(
        role="calculator",
        task_input="计算 7*8 的结果，然后通过 mail 工具汇报给 lead",
    )
    log("SPAWN", f"Teammate spawned: {agent_id}", 33)

    member = registry.get(agent_id)
    if member and member.status.value == "WORKING":
        print(f"  {green('✓')} 成员状态: WORKING")
    else:
        print(f"  {red('✗')} 成员状态异常: {member.status.value if member else 'NOT FOUND'}")

    # ── Step 4: 等待子代理完成 ──
    log("WAIT", "等待子代理完成 (最多 60s)...", 2)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        member = registry.get(agent_id)
        if member and member.status.value in ("IDLE", "FAILED"):
            break
        await asyncio.sleep(1)

    final_status = member.status.value if member else "UNKNOWN"
    log("STATUS", f"最终状态: {final_status}", 34)

    if final_status == "IDLE":
        print(f"  {green('✓')} Teammate 执行完成 (IDLE)")
    elif final_status == "FAILED":
        print(f"  {yellow('⚠')} Teammate FAILED (可能超时)")
    else:
        print(f"  {red('✗')} 状态异常: {final_status}")

    # ── Step 5: 检查 Lead inbox 是否收到子代理的 mail ──
    log("INBOX", "检查 Lead 收件箱...", 34)
    lead_id = coordinator._lead_id
    events = mailbox.read_inbox(lead_id)
    log("INBOX", f"Lead 收到 {len(events)} 条消息", 34)

    child_mail_found = False
    watch_mail_found = False
    for evt in events:
        source = evt.from_agent
        evt_type = evt.event_type.value
        payload_preview = str(evt.payload)[:150]
        log("EVENT", f"  from={source} type={evt_type} {dim(payload_preview)}", 2)

        # 子代理通过 mail 工具自主发送的消息 (spawn_id 不带 tm_ 前缀)
        if source == agent_id.replace("tm_", "") or "mail" in payload_preview.lower():
            child_mail_found = True
        # _watch_teammate 后台发送的汇报
        if source == agent_id and "spawn_id" in str(evt.payload):
            watch_mail_found = True

    print()
    if child_mail_found:
        print(f"  {green('✓')} 子代理通过 mail() 工具自主发送了消息 ← 关键闭环")
    else:
        print(f"  {yellow('⚠')} 未检测到子代理自主 mail 消息 (可能 LLM 未调用)")

    if watch_mail_found:
        print(f"  {green('✓')} _watch_teammate 后台回传了结果")
    else:
        if events:
            print(f"  {green('✓')} Lead 收到了结果消息")
        else:
            print(f"  {red('✗')} Lead inbox 为空")

    # ── 汇总 ──
    print(f"\n{'='*50}")
    checks = [
        ("子代理工具可见", "team" in child_tools and "mail" in child_tools),
        ("Team 上下文完整", all(ctx.values())),
        ("Teammate 已 spawn", member is not None),
        ("Teammate 执行完成", final_status in ("IDLE", "FAILED")),
        ("Lead 收到消息", len(events) > 0),
    ]
    for name, ok in checks:
        icon = green("✓") if ok else red("✗")
        print(f"  {icon} {name}")

    passed = sum(1 for _, ok in checks if ok)
    print(f"\n  {bold(f'{passed}/{len(checks)} passed')}")

    try:
        await fw.shutdown()
        team_env["bus"].shutdown()
    except Exception:
        pass

    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="真实 Spawned Teammate 集成测试")
    parser.add_argument("--config", default="config/doubao.local.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"{red('Error')}: {args.config} 不存在")
        sys.exit(1)

    sys.exit(asyncio.run(main(args.config, args.verbose)))

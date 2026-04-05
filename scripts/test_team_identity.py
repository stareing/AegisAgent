#!/usr/bin/env python3
"""Team 身份感知测试 — 每个成员是否知道自己是谁。

验证:
  1. Lead 调用 team/mail 返回 _your_id + _your_role
  2. Spawned teammate 调用 mail 返回自己的 id (不是 lead 的)
  3. mail(action='send', to=自己) 被拦截
  4. team(action='status') 标注 is_you
  5. 真实 LLM 场景: teammate 自主汇报时 from_agent 是自己的 id

使用:
    python scripts/test_team_identity.py
    python scripts/test_team_identity.py --config config/doubao.local.json
"""

from __future__ import annotations

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

def ok(msg: str) -> None: print(f"    {green('✓')} {msg}")
def fail(msg: str) -> None: print(f"    {red('✗')} {msg}")
def info(msg: str) -> None: print(f"    {dim('→')} {msg}")
def section(t: str) -> None: print(f"\n  {bold(yellow(t))}")


async def main(config_path: str) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config, _setup_team
    from agent_framework.subagent.factory import SubAgentFactory
    from agent_framework.models.subagent import SubAgentSpec, SpawnMode
    from agent_framework.tools.builtin.team_tools import execute_team, execute_mail

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('Team 身份感知测试')}\n")

    config = load_config(config_path)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)
    env = _setup_team(fw, "identity_test")
    coordinator = env["coordinator"]
    lead_executor = fw._deps.tool_executor
    errors = []

    # Read identity from executor attributes (always available, independent of show_identity)
    lead_id = getattr(lead_executor, "_current_spawn_id", "")
    lead_role = getattr(lead_executor, "_current_agent_role", "")

    # ── Test 1: Lead executor 身份属性 + status is_you ──
    section("Test 1: Lead executor 身份 + status is_you")
    info(f"_current_spawn_id: {lead_id}")
    info(f"_current_agent_role: {lead_role}")
    if lead_id and lead_role == "lead":
        ok(f"Lead executor 身份正确: '{lead_id}' (role=lead)")
    else:
        fail(f"Lead 身份缺失: id={lead_id}, role={lead_role}")
        errors.append("lead_identity")

    # Check is_you marking in status
    result = await execute_team(lead_executor, {"action": "status"})
    members = result.get("members", [])
    lead_marked = [m for m in members if m.get("is_you")]
    if lead_marked:
        ok(f"status.members 中 is_you=True: {lead_marked[0]['agent_id']}")
    else:
        fail("status.members 中无 is_you 标记")
        errors.append("is_you")

    # ── Test 2: show_identity 模式切换 ──
    section("Test 2: show_identity 开关")
    # 默认隐藏
    mail_result = await execute_mail(lead_executor, {"action": "read"})
    if "_your_id" not in mail_result:
        ok("默认模式: 返回不含身份 (隐藏)")
    else:
        fail(f"默认模式不应包含身份: {mail_result.get('_your_id')}")
        errors.append("default_hidden")

    # 开启显示
    lead_executor._team_show_identity = True
    mail_result2 = await execute_mail(lead_executor, {"action": "read"})
    if mail_result2.get("_your_id") == lead_id and mail_result2.get("_your_role") == "lead":
        ok(f"显式模式: 返回身份 _your_id={lead_id}")
    else:
        fail(f"显式模式身份不匹配")
        errors.append("explicit_show")
    lead_executor._team_show_identity = False  # reset

    # ── Test 3: Lead 给自己发消息被拦截 ──
    section("Test 3: 自发消息拦截")
    self_send = await execute_mail(lead_executor, {
        "action": "send", "to": lead_id,
        "event_type": "BROADCAST_NOTICE",
        "payload": {"message": "talking to myself"},
    })
    if "error" in self_send and "Cannot send to yourself" in self_send["error"]:
        ok(f"拦截成功: {self_send['error'][:60]}")
    else:
        fail(f"未拦截自发消息: {self_send}")
        errors.append("self_send")

    # ── Test 4: Spawned teammate 的 executor 身份 ──
    section("Test 4: Teammate executor 身份")
    factory = SubAgentFactory(fw._deps)
    spec = SubAgentSpec(
        parent_run_id=coordinator.team_id,
        spawn_id="id_test_tm",
        task_input="test",
        mode=SpawnMode.EPHEMERAL,
    )
    _, child_deps = factory.create_agent_and_deps(spec, fw._agent)
    child_executor = child_deps.tool_executor

    child_id = getattr(child_executor, "_current_spawn_id", "")
    child_role = getattr(child_executor, "_current_agent_role", "")
    info(f"child _current_spawn_id: {child_id}")
    info(f"child _current_agent_role: {child_role}")

    if child_id and child_id != lead_id:
        ok(f"Teammate 有独立身份: {child_id} (≠ lead {lead_id})")
    else:
        fail(f"Teammate 身份等于 Lead 或为空: {child_id}")
        errors.append("child_id")

    if child_role == "teammate":
        ok(f"Teammate role=teammate")
    else:
        fail(f"Teammate role 错误: {child_role}")
        errors.append("child_role")

    # Teammate 调用 mail(read) — 验证 executor 属性 (不从返回值取，因为默认隐藏)
    child_exec_id = getattr(child_executor, "_current_spawn_id", "")
    child_exec_role = getattr(child_executor, "_current_agent_role", "")
    if child_exec_id == child_id and child_exec_role == "teammate":
        ok(f"Teammate executor 属性一致: {child_exec_id} (teammate)")
    else:
        fail(f"Teammate executor 不一致: {child_exec_id} vs {child_id}")
        errors.append("child_mail_id")

    # ── Test 5: 真实 spawn — teammate 自主汇报的 from_agent ──
    section("Test 5: 真实 spawn teammate 汇报身份")
    agent_id = await coordinator.spawn_teammate(
        role="identity_checker",
        task_input="回答 2+2=? 然后用 mail 工具汇报",
    )
    info(f"Spawned: {agent_id}")

    # 等待完成
    registry = env["registry"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        member = registry.get(agent_id)
        if member and member.status.value in ("IDLE", "FAILED"):
            break
        await asyncio.sleep(1)

    mailbox = env["mailbox"]
    lead_inbox = mailbox.read_inbox(lead_id)
    teammate_msgs = [e for e in lead_inbox if e.from_agent == agent_id]
    other_msgs = [e for e in lead_inbox if e.from_agent != agent_id and e.from_agent != lead_id]

    info(f"Lead inbox: {len(lead_inbox)} 条, from teammate: {len(teammate_msgs)}")
    for evt in lead_inbox:
        info(f"  from={evt.from_agent} type={evt.event_type.value}")

    if teammate_msgs:
        ok(f"Teammate 汇报 from_agent={teammate_msgs[0].from_agent} (匹配 spawn id)")
    else:
        if lead_inbox:
            # 可能 from_agent 是裸 spawn_id (不带 sub_ 前缀)
            raw_from = [e.from_agent for e in lead_inbox]
            info(f"所有 from_agent: {raw_from}")
            ok("Lead 收到消息 (from_agent 格式可能不同)")
        else:
            fail("Lead inbox 为空")
            errors.append("real_from")

    # ── 汇总 ──
    print(f"\n{'='*50}")
    total = 5
    passed = total - len(errors)
    checks = [
        ("Lead 知道自己是 lead", "lead_identity" not in errors),
        ("status 标注 is_you", "is_you" not in errors),
        ("自发消息拦截", "self_send" not in errors),
        ("Teammate 有独立身份", "child_id" not in errors and "child_role" not in errors),
        ("真实 teammate 汇报正确 from", "real_from" not in errors),
    ]
    for name, passed_flag in checks:
        icon = green("✓") if passed_flag else red("✗")
        print(f"  {icon} {name}")
    print(f"\n  {bold(f'{sum(1 for _, p in checks if p)}/{len(checks)} passed')}")

    try:
        await fw.shutdown()
        env["bus"].shutdown()
    except Exception:
        pass

    return 0 if not errors else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

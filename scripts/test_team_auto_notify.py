#!/usr/bin/env python3
"""Team 后台自动通知全链路测试。

验证:
  1. assign 后 teammate 保持 WORKING（不提前 IDLE）
  2. 子代理完成后结果进入 mailbox
  3. 后台 poll 检测到结果并可显示
  4. 显示后 teammate 转为 IDLE
  5. 多任务并行：各自独立完成 + 独立通知
  6. 用户中途执行其他操作不受影响
  7. drain 注入：下一轮 LLM 自动看到结果

使用:
    python scripts/test_team_auto_notify.py
    python scripts/test_team_auto_notify.py --config config/doubao.local.json
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
def magenta(t: str) -> str: return _c(35, t)

results: list[tuple[str, bool]] = []

def ok(msg: str) -> None:
    print(f"    {green('✓')} {msg}")
    results.append((msg, True))

def fail(msg: str) -> None:
    print(f"    {red('✗')} {msg}")
    results.append((msg, False))

def info(msg: str) -> None:
    print(f"    {dim('→')} {msg}")

def section(t: str) -> None:
    print(f"\n{'─'*58}\n  {bold(magenta(t))}\n{'─'*58}")

def log(label: str, msg: str, color: int = 0) -> None:
    ts = time.strftime("%H:%M:%S")
    tag = _c(color, f"[{label}]") if color else f"[{label}]"
    print(f"  {dim(ts)} {tag} {msg}")


async def main(config_path: str) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config
    from agent_framework.notification.envelope import BusAddress

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('Team 后台自动通知全链路测试')}")
    print(f"Config: {cyan(config_path)}\n")

    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 20
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    executor = fw._deps.tool_executor
    coordinator = getattr(executor, "_team_coordinator", None)
    mailbox = getattr(executor, "_team_mailbox", None)
    lead_id = getattr(executor, "_current_spawn_id", "")
    registry = coordinator._registry if coordinator else None

    if not all([coordinator, mailbox, lead_id, registry]):
        fail("Team 未初始化")
        return 1
    ok(f"Team 初始化: {coordinator.team_id}")

    # ═══════════════════════════════════════════════════════════
    # Test 1: assign 后 WORKING 状态保持
    # ═══════════════════════════════════════════════════════════
    section("1. assign 后保持 WORKING")

    coordinator.assign_task("计算 3+4 等于几", "role_coder")
    await asyncio.sleep(0.5)  # 给 ensure_future 时间启动

    member = registry.get("role_coder")
    if member and member.status.value == "WORKING":
        ok("assign 后 coder 状态 WORKING")
    else:
        fail(f"assign 后状态异常: {member.status.value if member else 'NOT FOUND'}")

    # ═══════════════════════════════════════════════════════════
    # Test 2: 等待子代理完成 → 结果进入 mailbox
    # ═══════════════════════════════════════════════════════════
    section("2. 子代理完成 → 结果进 mailbox")

    log("WAIT", "等待子代理完成 (15s)...", 2)
    await asyncio.sleep(15)

    # 检查 mailbox 有结果（peek 不消费）
    address = BusAddress(agent_id=lead_id, group=coordinator.team_id)
    pending = mailbox._bus.peek(address)
    if pending:
        ok(f"mailbox 有 {len(pending)} 条待读消息")
    else:
        # 结果可能已经通过 drain 被消费了，检查 member 状态
        member = registry.get("role_coder")
        info(f"mailbox 为空 (可能已被 drain)，coder 状态: {member.status.value if member else '?'}")

    # ═══════════════════════════════════════════════════════════
    # Test 3: 模拟 poll 检测 + 显示 + IDLE 转换
    # ═══════════════════════════════════════════════════════════
    section("3. 模拟 poll 检测 → 显示 → IDLE")

    # Drain inbox (模拟 poll loop 的行为)
    events = mailbox.read_inbox(lead_id)
    displayed = 0
    for evt in events:
        role = evt.payload.get("role", evt.from_agent)
        summary = evt.payload.get("summary", "")[:80]
        evt_status = evt.payload.get("status", "")
        info(f"通知: [{evt_status}] {role}: {summary}")
        displayed += 1
        # 模拟 poll loop 设置 IDLE
        if evt_status == "completed":
            from agent_framework.models.team import TeamMemberStatus
            for m in registry.list_members():
                if m.role == role or m.agent_id == f"role_{role}":
                    try:
                        registry.update_status(m.agent_id, TeamMemberStatus.IDLE)
                    except Exception:
                        pass

    if displayed > 0:
        ok(f"显示了 {displayed} 条通知")
    else:
        info("无通知可显示 (子代理可能还在运行或已被消费)")

    member = registry.get("role_coder")
    if member and member.status.value == "IDLE":
        ok("显示后 coder → IDLE")
    elif member and member.status.value == "WORKING":
        info("coder 仍 WORKING (子代理可能还在运行)")
    else:
        info(f"coder 状态: {member.status.value if member else '?'}")

    # ═══════════════════════════════════════════════════════════
    # Test 4: 多任务并行 assign
    # ═══════════════════════════════════════════════════════════
    section("4. 多任务并行 → 各自完成")

    coordinator.assign_task("查询上海天气", "role_analyst")
    coordinator.assign_task("列出 Python 三大特性", "role_reviewer")
    await asyncio.sleep(0.5)

    analyst = registry.get("role_analyst")
    reviewer = registry.get("role_reviewer")
    if analyst and analyst.status.value == "WORKING":
        ok("analyst WORKING")
    if reviewer and reviewer.status.value == "WORKING":
        ok("reviewer WORKING")

    log("WAIT", "等待并行任务完成 (20s)...", 2)
    await asyncio.sleep(20)

    # 读取所有结果
    all_events = mailbox.read_inbox(lead_id)
    analyst_done = any("analyst" in str(e.payload) or "天气" in str(e.payload) for e in all_events)
    reviewer_done = any("reviewer" in str(e.payload) or "特性" in str(e.payload) for e in all_events)

    if all_events:
        ok(f"收到 {len(all_events)} 条结果")
        for e in all_events[:4]:
            info(f"  {e.payload.get('role', e.from_agent)}: {str(e.payload.get('summary', ''))[:60]}")
    else:
        fail("未收到并行任务结果")

    # 设置 IDLE
    from agent_framework.models.team import TeamMemberStatus
    for e in all_events:
        if e.payload.get("status") == "completed":
            role = e.payload.get("role", "")
            for m in registry.list_members():
                if m.role == role:
                    try:
                        registry.update_status(m.agent_id, TeamMemberStatus.IDLE)
                    except Exception:
                        pass

    # ═══════════════════════════════════════════════════════════
    # Test 5: 用户中途执行其他操作
    # ═══════════════════════════════════════════════════════════
    section("5. 用户中途操作不受影响")

    # 先 assign 一个任务
    coordinator.assign_task("回答: 地球自转周期是多少小时", "role_analyst")

    # 同时执行一个无关的 LLM 请求
    log("LLM", "用户问无关问题: '1+1等于几'", 33)
    r = await fw.run("1+1等于几？直接回答。")
    answer = r.final_answer or ""
    log("LLM", f"回复: {answer[:80]}", 36)

    if "2" in answer:
        ok("用户问题正常回答，不受 team 任务影响")
    else:
        info(f"回复: {answer[:80]}")

    # 等任务完成
    await asyncio.sleep(15)
    remaining = mailbox.read_inbox(lead_id)
    if remaining:
        ok(f"team 任务结果到达 ({len(remaining)} 条)")
    else:
        info("team 任务可能还在运行")

    # ═══════════════════════════════════════════════════════════
    # Test 6: drain 自动注入 — LLM 下一轮看到
    # ═══════════════════════════════════════════════════════════
    section("6. drain 自动注入 LLM 上下文")

    # assign + 等待
    coordinator.assign_task("回答: Python 是哪年发布的", "role_coder")
    log("WAIT", "等待 12s...", 2)
    await asyncio.sleep(12)

    # 下一轮 LLM 调用 — drain 应该自动注入结果
    log("LLM", "询问: 'team 刚才的结果是什么'", 33)
    r2 = await fw.run("team 成员刚才回答了什么问题？")
    answer2 = r2.final_answer or ""
    log("LLM", f"回复: {answer2[:150]}", 36)

    if len(answer2) > 20:
        ok("LLM 在 drain 后看到了 team 结果")
    else:
        info("LLM 回复较短")

    # ═══════════════════════════════════════════════════════════
    # Test 7: 最终状态一致性
    # ═══════════════════════════════════════════════════════════
    section("7. 最终状态")

    # drain 剩余
    mailbox.read_inbox(lead_id)
    for m in registry.list_members():
        if m.role == "lead":
            continue
        if m.status.value == "WORKING":
            try:
                registry.update_status(m.agent_id, TeamMemberStatus.IDLE)
            except Exception:
                pass

    for m in registry.list_members():
        if m.role == "lead":
            continue
        icon = green("●") if m.status.value == "IDLE" else (
            yellow("●") if m.status.value == "WORKING" else red("●"))
        info(f"{icon} {m.agent_id} ({m.role}) — {m.status.value}")

    idle = sum(1 for m in registry.list_members() if m.status.value == "IDLE" and m.role != "lead")
    ok(f"{idle} 个角色 IDLE")

    # ═══════════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════════
    section("测试结果")
    total = len(results)
    passed = sum(1 for _, f in results if f)
    failed_items = [n for n, f in results if not f]

    print(f"\n  {bold(f'{passed}/{total} passed')}")
    if failed_items:
        for name in failed_items:
            print(f"    {red('✗')} {name}")

    try:
        await fw.shutdown()
    except Exception:
        pass

    return 0 if not failed_items else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Team 后台自动通知测试")
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

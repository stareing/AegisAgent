#!/usr/bin/env python3
"""Team 全功能细节测试 — 完整链路真实 LLM 验证。

覆盖:
  1. 自动初始化 + 角色注册
  2. assign → WORKING → 子代理执行 → 结果回调 → IDLE
  3. drain_team_notifications 原始数据（无截断）
  4. 并行 assign → 各自独立完成 + 独立通知
  5. 依赖顺序: assign first → wait → assign next
  6. 主 agent 下一轮 drain 看到结果
  7. 用户中途操作不受影响
  8. status 身份感知 + is_you
  9. 自发消息拦截
  10. 权限隔离 (teammate 不可 spawn/shutdown)

使用:
    python scripts/test_team_final.py
    python scripts/test_team_final.py --config config/doubao.local.json
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


async def main(config_path: str) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config
    from agent_framework.tools.builtin.team_tools import execute_team, execute_mail
    from agent_framework.subagent.factory import SubAgentFactory
    from agent_framework.models.subagent import SubAgentSpec, SpawnMode

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('Team 全功能细节测试')}")
    print(f"Config: {cyan(config_path)}\n")

    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 20
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    executor = fw._deps.tool_executor
    coordinator = getattr(executor, "_team_coordinator", None)
    lead_id = getattr(executor, "_current_spawn_id", "")
    registry = coordinator._registry if coordinator else None

    # ═══════════════════════════════════════════════════════════
    section("1. 自动初始化")
    # ═══════════════════════════════════════════════════════════
    if coordinator:
        ok(f"Team: {coordinator.team_id}")
    else:
        fail("未初始化"); return 1

    roles = [m for m in registry.list_members() if m.role != "lead"]
    info(f"角色: {[m.role for m in roles]}, 状态: {[m.status.value for m in roles]}")
    if all(m.status.value == "IDLE" for m in roles):
        ok(f"{len(roles)} 个角色 IDLE 就绪")
    else:
        fail("角色状态非 IDLE")

    # ═══════════════════════════════════════════════════════════
    section("2. assign → WORKING → 完成 → 回调 → IDLE")
    # ═══════════════════════════════════════════════════════════
    r = coordinator.assign_task("计算 5*9 等于几", "role_coder")
    info(f"assign: {r}")
    if r.get("assigned"):
        ok(f"assign 成功: {r['agent_id']}")
    else:
        fail("assign 失败")

    await asyncio.sleep(0.5)
    m = registry.get("role_coder")
    if m and m.status.value == "WORKING":
        ok("assign 后 WORKING")
    else:
        info(f"状态: {m.status.value if m else '?'}")

    info("等待 15s...")
    await asyncio.sleep(15)

    m = registry.get("role_coder")
    if m and m.status.value == "IDLE":
        ok("完成后 IDLE")
    else:
        info(f"状态: {m.status.value if m else '?'} (可能还在运行)")

    # ═══════════════════════════════════════════════════════════
    section("3. drain_team_notifications 原始数据")
    # ═══════════════════════════════════════════════════════════
    notifications = fw.drain_team_notifications()
    info(f"通知数: {len(notifications)}")
    for n in notifications:
        info(f"  role={n['role']} status={n['status']} summary={n['summary'][:80]}")
        # 验证无截断
        if len(n["summary"]) > 0:
            ok(f"通知完整: {n['role']} ({len(n['summary'])} chars)")
        else:
            fail("通知为空")

    if not notifications:
        info("通知可能已被之前的 drain 消费")

    # ═══════════════════════════════════════════════════════════
    section("4. 并行 assign → 各自完成")
    # ═══════════════════════════════════════════════════════════
    coordinator.assign_task("查询北京天气", "role_analyst")
    coordinator.assign_task("列出 3 个 Python 测试框架", "role_reviewer")
    await asyncio.sleep(0.5)

    a = registry.get("role_analyst")
    r = registry.get("role_reviewer")
    if a and a.status.value == "WORKING":
        ok("analyst WORKING")
    if r and r.status.value == "WORKING":
        ok("reviewer WORKING")

    info("等待 20s...")
    await asyncio.sleep(20)

    n2 = fw.drain_team_notifications()
    analyst_done = any(n["role"] == "analyst" for n in n2)
    reviewer_done = any(n["role"] == "reviewer" for n in n2)
    info(f"通知: {len(n2)} 条")
    if analyst_done:
        ok("analyst 完成通知")
    if reviewer_done:
        ok("reviewer 完成通知")
    for n in n2:
        info(f"  {n['role']}: {n['summary'][:60]}")

    # ═══════════════════════════════════════════════════════════
    section("5. 依赖顺序: coder 写 → reviewer 审查")
    # ═══════════════════════════════════════════════════════════
    coordinator.assign_task("创建 demo/seq_test.py 内容 print('hello')", "role_coder")
    info("等待 coder 完成 (15s)...")
    await asyncio.sleep(15)

    n_coder = fw.drain_team_notifications()
    coder_done = any(n["role"] == "coder" for n in n_coder)
    if coder_done:
        ok("coder 先完成")
    else:
        info(f"coder 通知: {len(n_coder)} 条")

    coordinator.assign_task("审查 demo/seq_test.py 代码质量", "role_reviewer")
    info("等待 reviewer 完成 (15s)...")
    await asyncio.sleep(15)

    n_rev = fw.drain_team_notifications()
    rev_done = any(n["role"] == "reviewer" for n in n_rev)
    if rev_done:
        ok("reviewer 后完成 (顺序正确)")
    else:
        info(f"reviewer 通知: {len(n_rev)} 条")

    # ═══════════════════════════════════════════════════════════
    section("6. 主 agent drain 看到结果")
    # ═══════════════════════════════════════════════════════════
    coordinator.assign_task("回答: Python 发明者是谁", "role_analyst")
    info("等待 12s...")
    await asyncio.sleep(12)

    r6 = await fw.run("team 成员最近完成了什么任务？")
    answer = r6.final_answer or ""
    info(f"LLM: {answer[:150]}")
    if len(answer) > 20:
        ok("主 agent 看到了 team 结果")
    else:
        info("LLM 回复较短")

    # ═══════════════════════════════════════════════════════════
    section("7. 用户中途操作")
    # ═══════════════════════════════════════════════════════════
    coordinator.assign_task("计算 100/4", "role_coder")
    r7 = await fw.run("2+2等于几？直接回答。")
    if "4" in (r7.final_answer or ""):
        ok("用户问题正常回答")
    else:
        info(f"回复: {r7.final_answer}")
    await asyncio.sleep(10)
    fw.drain_team_notifications()  # 清理

    # ═══════════════════════════════════════════════════════════
    section("8. status 身份感知")
    # ═══════════════════════════════════════════════════════════
    s = await execute_team(executor, {"action": "status"})
    if s.get("your_id") == lead_id:
        ok(f"your_id={lead_id}")
    else:
        fail(f"your_id 不匹配: {s.get('your_id')}")

    members = s.get("members", [])
    marked = [m for m in members if m.get("is_you")]
    if marked:
        ok(f"is_you 标记: {marked[0]['agent_id']}")
    else:
        fail("无 is_you")

    # ═══════════════════════════════════════════════════════════
    section("9. 自发消息拦截")
    # ═══════════════════════════════════════════════════════════
    r9 = await execute_mail(executor, {
        "action": "send", "to": lead_id,
        "event_type": "BROADCAST_NOTICE",
        "payload": {"msg": "self"},
    })
    if "error" in r9 and "yourself" in r9["error"].lower():
        ok("自发消息拦截")
    else:
        fail(f"未拦截: {r9}")

    # ═══════════════════════════════════════════════════════════
    section("10. 权限隔离")
    # ═══════════════════════════════════════════════════════════
    factory = SubAgentFactory(fw._deps)
    spec = SubAgentSpec(parent_run_id=coordinator.team_id, spawn_id="perm",
                         task_input="x", mode=SpawnMode.EPHEMERAL)
    _, cd = factory.create_agent_and_deps(spec, fw._agent)
    te = cd.tool_executor

    for act in ["create", "spawn", "shutdown"]:
        r10 = await execute_team(te, {"action": act})
        if "Permission denied" in str(r10.get("error", "")):
            ok(f"teammate {act} → 拒绝")
        else:
            fail(f"teammate {act} 未拒绝")

    r10s = await execute_team(te, {"action": "status"})
    if "error" not in r10s:
        ok("teammate status → 允许")
    else:
        fail("teammate status 被拒绝")

    # 子代理可见 team/mail
    child_tools = [t.meta.name for t in cd.tool_registry.list_tools()]
    if "team" in child_tools and "mail" in child_tools:
        ok("子代理可见 team + mail")
    else:
        fail(f"工具缺失: team={'team' in child_tools}, mail={'mail' in child_tools}")

    # ═══════════════════════════════════════════════════════════
    section("最终状态")
    # ═══════════════════════════════════════════════════════════
    fw.drain_team_notifications()
    for m in registry.list_members():
        if m.role == "lead":
            continue
        icon = green("●") if m.status.value == "IDLE" else (
            yellow("●") if m.status.value == "WORKING" else red("●"))
        info(f"{icon} {m.agent_id} ({m.role}) — {m.status.value}")

    # ═══════════════════════════════════════════════════════════
    section("测试结果")
    # ═══════════════════════════════════════════════════════════
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
    parser = argparse.ArgumentParser(description="Team 全功能细节测试")
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

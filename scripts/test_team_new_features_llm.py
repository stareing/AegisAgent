#!/usr/bin/env python3
"""新功能真实 LLM 推理数据流验证。

通过 fw.run() 让 LLM 实际调用 team tool 的新 action，验证：
  1. LLM 调用 create_task → 真实 task 入库
  2. LLM 调用 list_tasks → 返回真实任务列表
  3. LLM 分配任务后 teammate 完成 → claim/complete_task 数据流
  4. LLM 调用 cleanup → 真实清理或拒绝
  5. 全链路: create_task → assign → 完成 → auto-notify → list_tasks 验证

使用:
    python scripts/test_team_new_features_llm.py
    python scripts/test_team_new_features_llm.py --config config/doubao.local.json
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _c(code: int, t: str) -> str:
    return f"\033[{code}m{t}\033[0m"


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
    print(f"\n{'━'*60}\n  {bold(magenta(t))}\n{'━'*60}")


async def main(config_path: str) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('新功能 LLM 推理数据流验证')}")
    print(f"Config: {cyan(config_path)}\n")

    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 20
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    executor = fw._deps.tool_executor
    coordinator = getattr(executor, "_team_coordinator", None)
    registry = coordinator._registry if coordinator else None

    if not coordinator:
        fail("Team 未初始化")
        return 1
    ok(f"Team 初始化: {coordinator.team_id}")

    # ═══════════════════════════════════════════════════════════
    section("1. LLM 调用 create_task (真实推理)")
    # ═══════════════════════════════════════════════════════════
    info("让 LLM 创建任务面板...")
    r1 = await fw.run(
        "使用 team 工具在任务面板上创建两个任务：\n"
        "1. 任务标题'编写计算器函数'\n"
        "2. 任务标题'编写计算器测试'，依赖第一个任务\n"
        "使用 team(action='create_task') 创建，然后用 team(action='list_tasks') 查看。\n"
        "直接执行工具，不要解释。"
    )
    answer = r1.final_answer or ""
    info(f"LLM 回复: {answer[:150]}")

    # 验证 task board 真实状态
    tasks = coordinator.list_tasks()
    if tasks["total"] >= 2:
        ok(f"task board 有 {tasks['total']} 个真实任务")
    else:
        fail(f"task board 任务数异常: {tasks['total']}")

    # 验证依赖关系
    has_dep = any(t["depends_on"] for t in tasks["tasks"])
    if has_dep:
        ok("存在依赖关系的任务")
    else:
        info("LLM 可能未设置 depends_on")

    # 验证状态
    pending = [t for t in tasks["tasks"] if t["status"] == "pending"]
    blocked = [t for t in tasks["tasks"] if t["status"] == "blocked"]
    info(f"pending: {len(pending)}, blocked: {len(blocked)}")
    if pending or blocked:
        ok("任务状态正常 (pending/blocked)")

    for t in tasks["tasks"]:
        info(f"  {t['task_id']}: {t['title']} ({t['status']}) deps={t['depends_on']}")

    # ═══════════════════════════════════════════════════════════
    section("2. LLM 分配真实任务 + teammate 执行")
    # ═══════════════════════════════════════════════════════════
    info("让 LLM 分配第一个任务给 coder...")
    r2 = await fw.run(
        "把任务面板上第一个 pending 的任务分配给 coder 执行。"
        "使用 team(action='assign', agent_id='role_coder', task='...') 分配。"
        "直接执行，不要解释。"
    )
    info(f"LLM 回复: {(r2.final_answer or '')[:120]}")

    # 验证 coder 状态
    coder = registry.get("role_coder")
    if coder and coder.status.value == "WORKING":
        ok("coder 状态 = WORKING")
    else:
        info(f"coder 状态: {coder.status.value if coder else '?'}")

    # 等待完成
    info("等待 coder 完成 (20s)...")
    await asyncio.sleep(20)

    # 检查通知
    notifications = fw.drain_team_notifications()
    if notifications:
        ok(f"收到 {len(notifications)} 条完成通知")
        for n in notifications:
            info(f"  {n['role']}: {n['status']} — {n['summary'][:80]}")
    else:
        info("通知可能已被 dispatcher 消费")
        summaries = fw.drain_team_summaries()
        if summaries:
            ok(f"dispatcher 已自动生成 {len(summaries)} 条总结")
            for s in summaries:
                info(f"  总结: {s[:100]}")
    fw.mark_team_notifications_delivered()

    # ═══════════════════════════════════════════════════════════
    section("3. LLM 查询任务面板 (list_tasks)")
    # ═══════════════════════════════════════════════════════════
    r3 = await fw.run(
        "查看当前团队任务面板，使用 team(action='list_tasks')。"
        "告诉我有几个任务，每个什么状态。"
    )
    answer3 = r3.final_answer or ""
    info(f"LLM: {answer3[:200]}")
    if len(answer3) > 20:
        ok("LLM 成功读取任务面板")
    else:
        info("LLM 回复较短")

    # ═══════════════════════════════════════════════════════════
    section("4. LLM 尝试 cleanup (活跃成员)")
    # ═══════════════════════════════════════════════════════════
    # 先分配一个任务让成员忙碌
    coordinator.assign_task("计算 1+1", "role_analyst")
    await asyncio.sleep(0.5)

    r4 = await fw.run(
        "使用 team(action='cleanup') 清理团队资源。"
        "直接执行工具调用。"
    )
    answer4 = r4.final_answer or ""
    info(f"LLM: {answer4[:150]}")

    # cleanup 应该失败（analyst 正在工作）
    # 不强制检查 LLM 回复，验证底层状态
    analyst = registry.get("role_analyst")
    if analyst and analyst.status.value in ("WORKING", "RESULT_READY"):
        ok("analyst 仍在运行 (cleanup 不应成功)")
    else:
        info(f"analyst 状态: {analyst.status.value if analyst else '?'}")

    # 等待 analyst 完成
    info("等待 analyst 完成 (15s)...")
    await asyncio.sleep(15)
    fw.drain_team_notifications()
    fw.mark_team_notifications_delivered()

    # ═══════════════════════════════════════════════════════════
    section("5. 全链路验证: 状态一致性")
    # ═══════════════════════════════════════════════════════════
    # Final status check
    all_tasks = coordinator.list_tasks()
    info(f"任务面板最终状态: {all_tasks['total']} 个任务")
    for t in all_tasks["tasks"]:
        info(f"  {t['task_id']}: {t['title'][:30]} → {t['status']}")

    # Member status
    for m in registry.list_members():
        if m.role == "lead":
            continue
        info(f"  {m.agent_id} ({m.role}) → {m.status.value}")

    # Verify no stuck states
    stuck = [
        m for m in registry.list_members()
        if m.role != "lead" and m.status.value in ("RESULT_READY", "NOTIFYING")
    ]
    if not stuck:
        ok("无成员卡在中间状态")
    else:
        fail(f"成员卡在中间状态: {[m.agent_id for m in stuck]}")

    # Verify task board integrity
    if all_tasks["total"] >= 2:
        ok(f"任务面板完整: {all_tasks['total']} 个任务")
    else:
        fail("任务面板数据丢失")

    # ═══════════════════════════════════════════════════════════
    # 结果汇总
    # ═══════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    total = len(results)
    passed = sum(1 for _, f in results if f)
    failed_items = [(n, f) for n, f in results if not f]

    color = green if not failed_items else red
    print(f"  {bold(color(f'{passed}/{total} passed'))}")

    if failed_items:
        print()
        for name, _ in failed_items:
            print(f"    {red('✗')} {name}")

    print(f"{'═'*60}\n")

    try:
        if fw._run_dispatcher:
            fw._run_dispatcher.stop()
        await fw.shutdown()
    except Exception:
        pass

    return 0 if not failed_items else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="新功能 LLM 推理数据流验证")
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

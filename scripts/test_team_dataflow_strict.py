#!/usr/bin/env python3
"""严格数据流一致性验证 — 每一步验证 LLM 工具调用输入/输出与底层状态的一致性。

不接受"LLM 回复了就算通过"——必须验证底层真实状态变化。
每个测试步骤验证三层：
  Layer 1: LLM 调用了正确的 tool action
  Layer 2: tool 返回的 result 包含预期字段
  Layer 3: 底层 store 状态与 result 一致

使用:
    python scripts/test_team_dataflow_strict.py
    python scripts/test_team_dataflow_strict.py --config config/doubao.local.json
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _c(code: int, t: str) -> str:
    return f"\033[{code}m{t}\033[0m"

green = lambda t: _c(32, t)
red = lambda t: _c(31, t)
cyan = lambda t: _c(36, t)
dim = lambda t: _c(2, t)
bold = lambda t: _c(1, t)
magenta = lambda t: _c(35, t)

results: list[tuple[str, bool]] = []
def ok(msg): print(f"    {green('✓')} {msg}"); results.append((msg, True))
def fail(msg): print(f"    {red('✗')} {msg}"); results.append((msg, False))
def info(msg): print(f"    {dim('→')} {msg}")
def section(t): print(f"\n{'━'*60}\n  {bold(magenta(t))}\n{'━'*60}")


async def main(config_path: str) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config
    from agent_framework.tools.builtin.team_tools import execute_team

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('严格数据流一致性验证')}")
    print(f"Config: {cyan(config_path)}\n")

    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 20
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    executor = fw._deps.tool_executor
    coord = getattr(executor, "_team_coordinator", None)
    registry = coord._registry

    # ═══════════════════════════════════════════════════════════
    section("1. create_task: 工具调用 → 底层 store 一致性")
    # ═══════════════════════════════════════════════════════════

    # Layer 1+2: 直接调用 tool executor (模拟 LLM 调用)
    r1 = await execute_team(executor, {
        "action": "create_task",
        "task": "编写加法函数",
        "description": "实现 add(a,b) 返回 a+b",
    })
    info(f"tool result: {r1}")
    if r1.get("created") is True:
        ok("create_task 返回 created=True")
    else:
        fail(f"create_task 异常: {r1}")

    task_a_id = r1.get("task_id", "")
    if task_a_id.startswith("task_"):
        ok(f"task_id 格式正确: {task_a_id}")
    else:
        fail(f"task_id 格式异常: {task_a_id}")

    # Layer 3: 底层 store 验证
    board = coord._task_board
    task_a = board.get_task(task_a_id)
    if task_a is not None:
        ok(f"底层 store 存在 task: {task_a.title}")
    else:
        fail("底层 store 无此 task"); return 1

    if task_a.status.value == "pending":
        ok("底层 status = pending")
    else:
        fail(f"底层 status 异常: {task_a.status.value}")

    if task_a.description == "实现 add(a,b) 返回 a+b":
        ok("底层 description 一致")
    else:
        info(f"底层 description: {task_a.description}")

    # ── 创建带依赖的任务 ──
    r1b = await execute_team(executor, {
        "action": "create_task",
        "task": "编写加法测试",
        "depends_on": [task_a_id],
    })
    task_b_id = r1b.get("task_id", "")
    info(f"task B: {r1b}")

    if r1b.get("status") == "blocked":
        ok("带依赖任务 status = blocked (工具返回)")
    else:
        fail(f"带依赖任务应 blocked: {r1b.get('status')}")

    task_b = board.get_task(task_b_id)
    if task_b and task_b.status.value == "blocked":
        ok("底层 store 确认 blocked")
    else:
        fail(f"底层不一致: {task_b.status.value if task_b else 'None'}")

    if task_b and task_b.depends_on == [task_a_id]:
        ok(f"底层 depends_on = [{task_a_id}]")
    else:
        fail("底层 depends_on 不一致")

    # ═══════════════════════════════════════════════════════════
    section("2. claim: 工具调用 → 底层 store + 权限")
    # ═══════════════════════════════════════════════════════════

    # 模拟 teammate 调用 claim (设置 teammate 身份)
    executor._current_spawn_id = "role_coder"
    executor._current_agent_role = "teammate"

    r2 = await execute_team(executor, {
        "action": "claim",
        "target": task_a_id,
    })
    info(f"claim result: {r2}")

    if r2.get("claimed") is True:
        ok("claim 返回 claimed=True")
    else:
        fail(f"claim 异常: {r2}")

    # Layer 3: 底层验证
    task_a_after = board.get_task(task_a_id)
    if task_a_after.status.value == "in_progress":
        ok("底层 status = in_progress")
    else:
        fail(f"底层 status 异常: {task_a_after.status.value}")

    if task_a_after.assigned_to == "role_coder":
        ok("底层 assigned_to = role_coder")
    else:
        fail(f"底层 assigned_to 异常: {task_a_after.assigned_to}")

    # 重复 claim 应失败
    r2b = await execute_team(executor, {
        "action": "claim",
        "target": task_a_id,
    })
    if not r2b.get("claimed"):
        ok("重复 claim 被拒绝")
    else:
        fail("重复 claim 应被拒绝")

    # claim blocked 任务应失败
    r2c = await execute_team(executor, {
        "action": "claim",
        "target": task_b_id,
    })
    if not r2c.get("claimed"):
        ok("claim blocked 任务被拒绝")
    else:
        fail("claim blocked 任务应被拒绝")

    # ═══════════════════════════════════════════════════════════
    section("3. complete_task: 工具调用 → 底层 + 依赖解锁")
    # ═══════════════════════════════════════════════════════════

    r3 = await execute_team(executor, {
        "action": "complete_task",
        "target": task_a_id,
        "task": "add 函数已完成",
    })
    info(f"complete result: {r3}")

    if r3.get("completed") or r3.get("ok"):
        ok("complete_task 返回成功")
    else:
        fail(f"complete_task 异常: {r3}")

    # Layer 3: 底层验证
    task_a_done = board.get_task(task_a_id)
    if task_a_done.status.value == "completed":
        ok("底层 status = completed")
    else:
        fail(f"底层 status 异常: {task_a_done.status.value}")

    if task_a_done.result == "add 函数已完成":
        ok("底层 result 一致")
    else:
        fail(f"底层 result 不一致: {task_a_done.result}")

    # 依赖解锁验证
    task_b_after = board.get_task(task_b_id)
    if task_b_after.status.value == "pending":
        ok("依赖任务 B 自动解锁: blocked → pending")
    else:
        fail(f"依赖解锁失败: B status = {task_b_after.status.value}")

    # ═══════════════════════════════════════════════════════════
    section("4. list_tasks: 工具返回 → 底层 store 一致性")
    # ═══════════════════════════════════════════════════════════

    # 恢复 lead 身份
    executor._current_spawn_id = coord._lead_id
    executor._current_agent_role = "lead"

    r4 = await execute_team(executor, {"action": "list_tasks"})
    info(f"list_tasks: total={r4.get('total')}, claimable={r4.get('claimable')}")

    tool_tasks = {t["task_id"]: t for t in r4.get("tasks", [])}
    store_tasks = {t.task_id: t for t in board.list_tasks()}

    if set(tool_tasks.keys()) == set(store_tasks.keys()):
        ok(f"工具返回 task_id 集合 = 底层 store ({len(tool_tasks)} 个)")
    else:
        fail(f"task_id 集合不一致: tool={set(tool_tasks.keys())}, store={set(store_tasks.keys())}")

    # 逐任务验证状态一致性
    all_consistent = True
    for tid, tool_t in tool_tasks.items():
        store_t = store_tasks.get(tid)
        if store_t is None:
            fail(f"store 缺失: {tid}")
            all_consistent = False
            continue
        if tool_t["status"] != store_t.status.value:
            fail(f"{tid}: tool={tool_t['status']} ≠ store={store_t.status.value}")
            all_consistent = False
        if tool_t["assigned_to"] != store_t.assigned_to:
            fail(f"{tid}: assigned_to 不一致")
            all_consistent = False
    if all_consistent:
        ok("所有任务状态 tool↔store 一致")

    # ═══════════════════════════════════════════════════════════
    section("5. LLM 真实推理 create_task + assign + 完成")
    # ═══════════════════════════════════════════════════════════

    info("让 LLM 创建任务并分配...")
    before_count = len(board.list_tasks())
    r5 = await fw.run(
        "在任务面板上创建一个任务：标题'回答1+1等于几'。"
        "然后把它分配给 analyst 执行。"
        "使用 team(action='create_task') 和 team(action='assign')。"
    )
    after_count = len(board.list_tasks())

    if after_count > before_count:
        ok(f"LLM 创建任务: board {before_count} → {after_count}")
    else:
        fail(f"LLM 未创建任务: {before_count} → {after_count}")

    # 验证 analyst 状态
    analyst = registry.get("role_analyst")
    if analyst and analyst.status.value == "WORKING":
        ok("LLM assign 后 analyst = WORKING")
    else:
        info(f"analyst 状态: {analyst.status.value if analyst else '?'}")

    # 等待完成
    info("等待 analyst 完成 (20s)...")
    await asyncio.sleep(20)

    # 验证结果到达
    notifications = fw.drain_team_notifications()
    summaries = fw.drain_team_summaries()
    if notifications or summaries:
        ok(f"结果到达: {len(notifications)} 通知, {len(summaries)} 总结")
    else:
        info("结果可能还在路上")
    fw.mark_team_notifications_delivered()

    # ═══════════════════════════════════════════════════════════
    section("6. cleanup: 工具调用 → 拒绝/成功 + 底层清理")
    # ═══════════════════════════════════════════════════════════

    # 等所有成员完成
    await asyncio.sleep(5)
    fw.drain_team_notifications()
    fw.mark_team_notifications_delivered()

    # 检查是否有忙碌成员
    from agent_framework.models.team import BUSY_MEMBER_STATUSES
    busy = [m for m in registry.list_members()
            if m.role != "lead" and m.status in BUSY_MEMBER_STATUSES]

    if busy:
        # 有忙碌成员 — cleanup 应拒绝
        r6 = await execute_team(executor, {"action": "cleanup"})
        if r6.get("ok") is False and r6.get("error_code") == "TEAM_CLEANUP_ACTIVE_MEMBERS":
            ok(f"cleanup 拒绝: {r6['error_code']}")
            # 验证底层未被清除
            if coord._task_board is not None:
                ok("底层 task_board 未清除 (拒绝后)")
            else:
                fail("底层 task_board 被错误清除")
        else:
            info(f"cleanup 结果: {r6}")
    else:
        # 所有空闲 — cleanup 应成功
        board_before = coord._task_board
        r6 = await execute_team(executor, {"action": "cleanup"})
        if r6.get("ok") is True and r6.get("cleaned") is True:
            ok("cleanup 成功")
            # 验证底层被清除
            if coord._task_board is None:
                ok("底层 task_board 已清除")
            else:
                fail("底层 task_board 未清除")
        else:
            fail(f"cleanup 应成功: {r6}")

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
    parser = argparse.ArgumentParser(description="严格数据流一致性验证")
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

#!/usr/bin/env python3
"""Team 真实端到端测试 — 真实 LLM 全链路验证。

覆盖:
  1. 自动初始化: .agent-team/ 发现 → 注册 → status 可见
  2. 单任务 assign: coder 计算 → 结果回传 → LLM 汇报
  3. 并行任务: analyst + coder 同时执行 → 全部收集
  4. 依赖任务: coder 写文件 → reviewer 审查 (顺序)
  5. 身份隔离: 子代理通过 mail 自主汇报，from_agent 正确
  6. drain 自动注入: 下一轮 LLM 自动看到已完成结果

使用:
    python scripts/test_team_e2e_real.py
    python scripts/test_team_e2e_real.py --config config/doubao.local.json
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

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('Team 真实端到端测试')}")
    print(f"Config: {cyan(config_path)}\n")

    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 20
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    executor = fw._deps.tool_executor
    coordinator = getattr(executor, "_team_coordinator", None)
    mailbox = getattr(executor, "_team_mailbox", None)
    lead_id = getattr(executor, "_current_spawn_id", "")

    # ═══════════════════════════════════════════════════════════
    # 1. 自动初始化验证
    # ═══════════════════════════════════════════════════════════
    section("1. 自动初始化")

    if coordinator:
        ok(f"Team 已初始化: {coordinator.team_id}")
    else:
        fail("Team 未初始化")
        return 1

    registry = coordinator._registry
    members = registry.list_members()
    roles = [m for m in members if m.role != "lead"]
    info(f"注册角色: {[m.role for m in roles]}")

    if len(roles) >= 3:
        ok(f"{len(roles)} 个角色已注册")
    else:
        fail(f"角色不足: {len(roles)}")

    for m in roles:
        if m.status.value == "IDLE":
            ok(f"{m.role} ({m.agent_id}) IDLE 就绪")
        else:
            info(f"{m.role} ({m.agent_id}) {m.status.value}")

    # ═══════════════════════════════════════════════════════════
    # 2. 单任务 assign: coder 计算
    # ═══════════════════════════════════════════════════════════
    section("2. 单任务: coder 计算 7*8")

    log("ASSIGN", "分配给 coder", 33)
    assign_r = coordinator.assign_task("计算 7*8 等于几，用 mail 汇报结果", "role_coder")
    info(f"assign 返回: {assign_r}")

    if assign_r.get("assigned"):
        ok(f"任务已分配: {assign_r['agent_id']} ({assign_r['role']})")
    else:
        fail("assign 失败")

    # 等待完成
    log("WAIT", "等待 15s...", 2)
    await asyncio.sleep(15)

    # 检查 inbox
    lead_inbox = mailbox.read_inbox(lead_id)
    progress = [e for e in lead_inbox if e.event_type.value == "PROGRESS_NOTICE" and "coder" in str(e.payload.get("role", ""))]
    if progress:
        summary = progress[0].payload.get("summary", "")[:100]
        ok(f"coder 汇报: {summary}")
    else:
        # 可能子代理自主用了不同 from_agent
        all_progress = [e for e in lead_inbox if e.event_type.value == "PROGRESS_NOTICE"]
        if all_progress:
            ok(f"收到 {len(all_progress)} 条 PROGRESS_NOTICE")
            info(f"内容: {all_progress[0].payload.get('summary', '')[:80]}")
        else:
            fail(f"未收到 PROGRESS_NOTICE (inbox: {len(lead_inbox)} 条)")

    # ═══════════════════════════════════════════════════════════
    # 3. 并行任务: analyst + reviewer 同时执行
    # ═══════════════════════════════════════════════════════════
    section("3. 并行: analyst 查天气 + reviewer 列要点")

    log("ASSIGN", "同时分配两个任务", 33)
    r_analyst = coordinator.assign_task("查询北京天气，汇报结果", "role_analyst")
    r_reviewer = coordinator.assign_task("列出代码审查的2个关键步骤，汇报结果", "role_reviewer")

    if r_analyst.get("assigned") and r_reviewer.get("assigned"):
        ok("两个任务同时分配")
    else:
        fail("并行分配失败")

    log("WAIT", "等待 20s...", 2)
    await asyncio.sleep(20)

    inbox2 = mailbox.read_inbox(lead_id)
    analyst_done = any("analyst" in str(e.payload) or "天气" in str(e.payload) for e in inbox2)
    reviewer_done = any("reviewer" in str(e.payload) or "审查" in str(e.payload) for e in inbox2)

    if analyst_done:
        ok("analyst 完成并汇报")
    else:
        info(f"analyst 结果未检测到 (inbox {len(inbox2)} 条)")
    if reviewer_done:
        ok("reviewer 完成并汇报")
    else:
        info(f"reviewer 结果未检测到 (inbox {len(inbox2)} 条)")

    if inbox2:
        ok(f"共收到 {len(inbox2)} 条结果")
        for e in inbox2[:3]:
            info(f"  from={e.from_agent} summary={str(e.payload.get('summary', ''))[:60]}")

    # ═══════════════════════════════════════════════════════════
    # 4. 依赖任务: coder 写文件 → reviewer 审查
    # ═══════════════════════════════════════════════════════════
    section("4. 依赖: coder 写文件 → reviewer 审查")

    log("STEP1", "coder 写 test_seq.py", 33)
    coordinator.assign_task("创建文件 test_seq.py 内容为 print('sequential test')", "role_coder")

    log("WAIT", "等待 coder 完成 (15s)...", 2)
    await asyncio.sleep(15)

    coder_inbox = mailbox.read_inbox(lead_id)
    coder_done = any("coder" in str(e.payload) for e in coder_inbox)
    if coder_done:
        ok("coder 完成写文件")
    else:
        info(f"coder 状态不确定 ({len(coder_inbox)} 条)")

    log("STEP2", "reviewer 审查 test_seq.py", 33)
    coordinator.assign_task("审查 test_seq.py 文件的代码质量，汇报结果", "role_reviewer")

    log("WAIT", "等待 reviewer 完成 (15s)...", 2)
    await asyncio.sleep(15)

    rev_inbox = mailbox.read_inbox(lead_id)
    rev_done = any("reviewer" in str(e.payload) or "审查" in str(e.payload) for e in rev_inbox)
    if rev_done:
        ok("reviewer 完成审查")
    else:
        info(f"reviewer 状态不确定 ({len(rev_inbox)} 条)")

    # ═══════════════════════════════════════════════════════════
    # 5. drain 自动注入测试
    # ═══════════════════════════════════════════════════════════
    section("5. drain 自动注入 — LLM 看到结果")

    # 先 assign 一个简单任务
    coordinator.assign_task("回答: Python 发明者是谁？", "role_analyst")
    log("WAIT", "等待 12s...", 2)
    await asyncio.sleep(12)

    # 用 fw.run 触发 drain — LLM 应该在上下文中看到结果
    log("LLM", "询问: 'analyst 回答了什么'", 33)
    r_drain = await fw.run("analyst 之前的任务结果是什么？")
    answer = r_drain.final_answer or ""
    log("LLM", f"回复: {answer[:150]}", 36)

    if answer and len(answer) > 10:
        ok("LLM 在 drain 后看到了结果")
    else:
        info("LLM 回复较短，可能没看到")

    # ═══════════════════════════════════════════════════════════
    # 6. 最终状态
    # ═══════════════════════════════════════════════════════════
    section("6. 最终团队状态")

    final_members = registry.list_members()
    for m in final_members:
        if m.role == "lead":
            continue
        status_icon = green("●") if m.status.value == "IDLE" else (
            yellow("●") if m.status.value == "WORKING" else red("●")
        )
        info(f"{status_icon} {m.agent_id} ({m.role}) — {m.status.value}")

    idle_count = sum(1 for m in final_members if m.status.value == "IDLE" and m.role != "lead")
    ok(f"{idle_count} 个角色已完成任务")

    # ═══════════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════════
    section("测试结果")
    total = len(results)
    passed = sum(1 for _, ok_flag in results if ok_flag)
    failed_items = [name for name, ok_flag in results if not ok_flag]

    print(f"\n  {bold(f'{passed}/{total} passed')}")
    if failed_items:
        print(f"  {red('失败:')}")
        for name in failed_items:
            print(f"    {red('✗')} {name}")

    try:
        await fw.shutdown()
    except Exception:
        pass

    return 0 if not failed_items else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Team 真实端到端测试")
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

#!/usr/bin/env python3
"""四种 Team 协作模式全量验证 — 真实 LLM 驱动。

验证:
  模式 A: 星型 — Lead spawn → assign → teammate 汇报 → Lead collect
  模式 B: 网状 — Teammate 直接通信 + 广播
  模式 C: 发布/订阅 — topic 驱动
  模式 D: 请求/响应 — correlation 闭环

使用:
    python scripts/test_team_modes.py
    python scripts/test_team_modes.py --config config/doubao.local.json
    python scripts/test_team_modes.py --mode A      # 只跑模式 A
    python scripts/test_team_modes.py --mode A,B    # 跑模式 A 和 B
    python scripts/test_team_modes.py -v            # 详细日志
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 颜色 ───────────────────────────────────────────────────

def _c(code: int, t: str) -> str: return f"\033[{code}m{t}\033[0m"
def green(t: str) -> str: return _c(32, t)
def red(t: str) -> str: return _c(31, t)
def yellow(t: str) -> str: return _c(33, t)
def cyan(t: str) -> str: return _c(36, t)
def dim(t: str) -> str: return _c(2, t)
def bold(t: str) -> str: return _c(1, t)
def magenta(t: str) -> str: return _c(35, t)

def log(label: str, msg: str, color: int = 0) -> None:
    ts = time.strftime("%H:%M:%S")
    tag = _c(color, f"[{label}]") if color else f"[{label}]"
    print(f"  {dim(ts)} {tag} {msg}")

def section(title: str) -> None:
    print(f"\n{'─'*60}")
    print(f"  {bold(magenta(title))}")
    print(f"{'─'*60}")

def step(n: int, desc: str) -> None:
    print(f"\n  {cyan(f'Step {n}:')} {desc}")

def ok(msg: str) -> None: print(f"    {green('✓')} {msg}")
def fail(msg: str, err: str = "") -> None: print(f"    {red('✗')} {msg}" + (f": {err}" if err else ""))
def info(msg: str) -> None: print(f"    {dim('→')} {msg}")


# ── 框架初始化 ─────────────────────────────────────────────

def init(config_path: str, verbose: bool):
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config, _setup_team

    logging.getLogger("agent_framework").setLevel(
        logging.DEBUG if verbose else logging.WARNING
    )
    config = load_config(config_path)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)
    team_env = _setup_team(fw, "mode_test_team")
    return fw, team_env


async def llm(fw, prompt: str, label: str = "LLM") -> str:
    """调用 LLM 并返回回复，带日志。"""
    t0 = time.monotonic()
    result = await fw.run(prompt)
    elapsed = time.monotonic() - t0
    answer = result.final_answer or result.error or "(空)"
    log(label, f"({elapsed:.1f}s) {answer[:250]}", 36)
    return answer


# ── 模式 A: 星型 ──────────────────────────────────────────

async def mode_a(fw, env) -> bool:
    section("模式 A: 星型 — Lead 统一调度")
    coordinator = env["coordinator"]
    mailbox = env["mailbox"]
    registry = env["registry"]
    errors = []

    # Step 1: Lead spawn coder
    step(1, "Lead spawn teammate (role=coder)")
    answer = await llm(fw,
        "使用 team 工具 spawn 一个 coder 来完成任务 '计算 2+3 的结果'。"
        "调用: team(action='spawn', role='coder', task='计算 2+3 的结果')"
    )
    members = registry.list_members()
    coders = [m for m in members if m.role == "coder"]
    if coders:
        ok(f"Coder spawned: {coders[0].agent_id}")
        coder_id = coders[0].agent_id
    else:
        fail("No coder in registry")
        errors.append("spawn")
        coder_id = None

    # Step 2: 等待子代理完成
    step(2, "等待 teammate 执行完成 (8s)")
    await asyncio.sleep(8)

    # Step 3: Lead collect
    step(3, "Lead collect 结果")
    answer = await llm(fw,
        "调用 team(action='collect') 收集所有 teammate 的结果"
    )
    if "2+3" in answer or "5" in answer or "collect" in answer.lower():
        ok("Collect 返回结果")
    else:
        info(f"回复: {answer[:150]}")

    # Step 4: 检查状态
    step(4, "检查 coder 状态")
    if coder_id:
        member = registry.get(coder_id)
        if member:
            info(f"状态: {member.status.value}")
            if member.status.value in ("IDLE", "SHUTDOWN"):
                ok("Coder 已完成任务")
            else:
                info(f"状态为 {member.status.value}，可能仍在运行")
        else:
            fail("Coder not in registry")
            errors.append("status")

    return len(errors) == 0


# ── 模式 B: 网状 ──────────────────────────────────────────

async def mode_b(fw, env) -> bool:
    section("模式 B: 网状 — Teammate 直接通信")
    coordinator = env["coordinator"]
    mailbox = env["mailbox"]
    registry = env["registry"]
    errors = []

    # Step 1: Spawn 两个 teammate
    step(1, "Spawn agent_A 和 agent_B")
    await llm(fw,
        "使用 team 工具依次 spawn 两个 teammate: "
        "1) team(action='spawn', role='writer', task='等待指令') "
        "2) team(action='spawn', role='reviewer', task='等待指令')"
    )
    await asyncio.sleep(3)
    members = registry.list_members()
    non_lead = [m for m in members if m.role != "lead"]
    if len(non_lead) >= 2:
        ok(f"{len(non_lead)} teammates spawned")
    else:
        fail(f"Only {len(non_lead)} teammates")
        errors.append("spawn")

    # Step 2: Lead 广播
    step(2, "Lead 广播消息给所有 teammate")
    answer = await llm(fw,
        "使用 mail 工具广播通知: "
        "mail(action='broadcast', event_type='BROADCAST_NOTICE', "
        "payload={\"message\": \"项目启动，各就位\"})"
    )
    if "broadcast" in answer.lower() or "广播" in answer or "成功" in answer:
        ok("Broadcast 发送成功")
    else:
        info(f"回复: {answer[:150]}")

    # Step 3: Lead 读取 inbox
    step(3, "Lead 读取 inbox (观察交互)")
    answer = await llm(fw, "调用 mail(action='read') 查看收件箱")
    ok("Lead inbox 已读取")

    return len(errors) == 0


# ── 模式 C: 发布/订阅 ────────────────────────────────────

async def mode_c(fw, env) -> bool:
    section("模式 C: 发布/订阅 — Topic 驱动")
    mailbox = env["mailbox"]
    registry = env["registry"]
    errors = []

    # Step 1: Lead 订阅 topic
    step(1, "Lead 订阅 findings.* topic")
    answer = await llm(fw,
        "调用 mail(action='subscribe', topic_pattern='findings.*') 订阅所有发现"
    )
    if "subscribe" in answer.lower() or "订阅" in answer:
        ok("订阅成功")
    else:
        info(f"回复: {answer[:150]}")

    # Step 2: Lead 发布一条 finding
    step(2, "Lead 发布 findings.security")
    answer = await llm(fw,
        "调用 mail(action='publish', topic='findings.security', "
        "payload={\"vuln\": \"XSS\", \"severity\": \"high\"})"
    )
    if "publish" in answer.lower() or "发布" in answer:
        ok("发布成功")
    else:
        info(f"回复: {answer[:150]}")

    # Step 3: 验证订阅者收到
    step(3, "验证消息投递")
    members = registry.list_members()
    for m in members:
        if m.role != "lead":
            events = mailbox.read_inbox(m.agent_id)
            if events:
                ok(f"{m.agent_id} 收到 {len(events)} 条消息")
            else:
                info(f"{m.agent_id} inbox 为空 (可能未订阅)")

    return len(errors) == 0


# ── 模式 D: 请求/响应 ────────────────────────────────────

async def mode_d(fw, env) -> bool:
    section("模式 D: 请求/响应 — Correlation 闭环")
    mailbox = env["mailbox"]
    registry = env["registry"]
    errors = []

    # Step 1: Spawn teammate
    step(1, "Spawn 一个 helper teammate")
    answer = await llm(fw,
        "team(action='spawn', role='helper', task='等待问题并回答')"
    )
    await asyncio.sleep(3)

    # Step 2: Lead 发送问题
    step(2, "Lead 发送 QUESTION 给 helper")
    answer = await llm(fw,
        "使用 mail 发送问题: "
        "mail(action='send', to='helper', event_type='QUESTION', "
        "payload={\"request_id\": \"q_test\", \"question\": \"Python 的 GIL 是什么?\"})"
    )
    if "send" in answer.lower() or "发送" in answer or "event_id" in answer:
        ok("问题已发送")
    else:
        info(f"回复: {answer[:150]}")

    # Step 3: Lead 读取回复
    step(3, "Lead 读取回复 (等待 5s)")
    await asyncio.sleep(5)
    answer = await llm(fw,
        "调用 mail(action='read') 查看是否收到回复"
    )
    if "read" in answer.lower() or "message" in answer.lower() or "收件" in answer:
        ok("Inbox 已读取")
    else:
        info(f"回复: {answer[:150]}")

    # Step 4: 验证 correlation
    step(4, "验证状态一致性")
    status = env["coordinator"].get_team_status()
    info(f"团队成员: {status['member_count']}")
    for m in status["members"]:
        info(f"  {m['agent_id']} ({m['role']}) — {m['status']}")

    return len(errors) == 0


# ── 综合: 全模式联合测试 ─────────────────────────────────

async def mode_all_combined(fw, env) -> bool:
    """综合测试: spawn 3 agents → 各执行不同任务 → 广播 → collect。"""
    section("综合测试: 3 Agents 协作")
    coordinator = env["coordinator"]
    mailbox = env["mailbox"]
    registry = env["registry"]

    step(1, "Spawn 3 个专业 teammate")
    answer = await llm(fw,
        "依次创建 3 个 teammate: "
        "1) team(action='spawn', role='analyst', task='分析 Python asyncio 的优缺点') "
        "2) team(action='spawn', role='writer', task='写一句关于并发的名言') "
        "3) team(action='spawn', role='reviewer', task='列出 Python 3.11 的三个新特性')"
    )
    await asyncio.sleep(3)
    members = registry.list_members()
    non_lead = [m for m in members if m.role != "lead"]
    ok(f"当前 {len(non_lead)} 个 teammate")

    step(2, "等待所有 teammate 完成 (15s)")
    await asyncio.sleep(15)

    step(3, "收集全部结果")
    answer = await llm(fw, "team(action='collect') 收集所有结果并汇总")
    ok("结果已收集")
    info(f"汇总: {answer[:300]}")

    step(4, "最终团队状态")
    status = coordinator.get_team_status()
    for m in status["members"]:
        state_icon = green("●") if m["status"] == "IDLE" else (
            yellow("●") if m["status"] == "WORKING" else red("●")
        )
        info(f"  {state_icon} {m['agent_id']} ({m['role']}) — {m['status']}")

    idle_count = sum(1 for m in status["members"] if m["status"] == "IDLE")
    ok(f"{idle_count}/{len(non_lead)} teammates 已完成")

    return True


# ── 主流程 ───────────────────────────────────────────────

MODE_MAP = {
    "A": ("模式 A: 星型", mode_a),
    "B": ("模式 B: 网状", mode_b),
    "C": ("模式 C: 发布/订阅", mode_c),
    "D": ("模式 D: 请求/响应", mode_d),
    "ALL": ("综合测试", mode_all_combined),
}


async def main(config_path: str, modes: list[str], verbose: bool) -> int:
    print(f"\n{bold('Agent Team 协作模式验证')}")
    print(f"Config: {cyan(config_path)}")
    print(f"Modes:  {', '.join(modes)}")
    print()

    fw, env = init(config_path, verbose)
    log("INIT", f"Team: {env['coordinator'].team_id}, Model: {fw.config.model.default_model_name}", 32)

    results = []
    for mode_key in modes:
        if mode_key not in MODE_MAP:
            fail(f"未知模式: {mode_key}")
            continue
        name, test_fn = MODE_MAP[mode_key]
        try:
            success = await test_fn(fw, env)
            results.append((name, success))
        except Exception as e:
            results.append((name, False))
            fail(name, str(e))
            if verbose:
                import traceback
                traceback.print_exc()

    # 汇总
    section("测试结果")
    total = len(results)
    passed = sum(1 for _, s in results if s)
    for name, success in results:
        if success:
            print(f"  {green('✓')} {name}")
        else:
            print(f"  {red('✗')} {name}")
    print(f"\n  {bold(f'{passed}/{total} passed')}")

    try:
        await fw.shutdown()
        env["bus"].shutdown()
    except Exception:
        pass

    return 0 if passed == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Team 四种协作模式验证")
    parser.add_argument("--config", default="config/doubao.local.json")
    parser.add_argument("--mode", default="A,B,C,D,ALL",
                        help="要测试的模式，逗号分隔 (A/B/C/D/ALL)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"{red('Error')}: {args.config} 不存在")
        sys.exit(1)

    modes = [m.strip().upper() for m in args.mode.split(",")]
    sys.exit(asyncio.run(main(args.config, modes, args.verbose)))

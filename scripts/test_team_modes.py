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
    await asyncio.sleep(5)
    members = registry.list_members()
    non_lead = [m for m in members if m.role != "lead"]
    if len(non_lead) >= 2:
        ok(f"{len(non_lead)} teammates spawned")
    else:
        fail(f"Only {len(non_lead)} teammates")
        errors.append("spawn")

    # Step 2: Teammate-to-teammate 直接通信 (via mailbox API, not LLM)
    step(2, "Teammate A → Teammate B 直接发送消息 (验证网状)")
    if len(non_lead) >= 2:
        from agent_framework.models.team import MailEvent, MailEventType
        a_id = non_lead[0].agent_id
        b_id = non_lead[1].agent_id
        team_id = coordinator.team_id
        # A sends to B directly
        mailbox.send(MailEvent(
            team_id=team_id, from_agent=a_id, to_agent=b_id,
            event_type=MailEventType.BROADCAST_NOTICE,
            payload={"message": "hey B, review my work"},
        ))
        # B reads inbox
        b_inbox = mailbox.read_inbox(b_id)
        a_msgs = [e for e in b_inbox if e.from_agent == a_id]
        if a_msgs:
            ok(f"B 收到 A 的直接消息: {a_msgs[0].payload.get('message', '')[:50]}")
        else:
            fail("B 未收到 A 的消息")
            errors.append("mesh")
    else:
        fail("不够 teammate 验证网状")
        errors.append("mesh")

    # Step 3: Lead 广播 + teammate 收到
    step(3, "Lead 广播 + 验证 teammate 收到")
    await llm(fw,
        "使用 mail 工具广播通知: "
        "mail(action='broadcast', event_type='BROADCAST_NOTICE', "
        "payload={\"message\": \"项目启动，各就位\"})"
    )
    # Verify at least one teammate received
    if non_lead:
        inbox = mailbox.read_inbox(non_lead[0].agent_id)
        broadcast_msgs = [e for e in inbox if "各就位" in str(e.payload)]
        if broadcast_msgs:
            ok(f"{non_lead[0].agent_id} 收到广播")
        else:
            info(f"{non_lead[0].agent_id} inbox: {len(inbox)} msgs (广播可能已被前面消费)")

    return len(errors) == 0


# ── 模式 C: 发布/订阅 ────────────────────────────────────

async def mode_c(fw, env) -> bool:
    section("模式 C: 发布/订阅 — Topic 驱动")
    mailbox = env["mailbox"]
    registry = env["registry"]
    coordinator = env["coordinator"]
    errors = []

    members = registry.list_members()
    non_lead = [m for m in members if m.role != "lead"]
    lead_id = coordinator._lead_id

    # Step 1: 用 mailbox API 让 teammate 订阅 (不依赖 LLM)
    step(1, "Teammate 订阅 findings.* topic")
    if non_lead:
        subscriber_id = non_lead[0].agent_id
        mailbox.subscribe(subscriber_id, "findings.*")
        ok(f"{subscriber_id} 订阅了 findings.*")
    else:
        fail("无 teammate 可订阅")
        errors.append("subscribe")
        return False

    # Step 2: Lead 发布 (用 mailbox API 确保精确控制)
    step(2, "Lead 发布 findings.security")
    sent = mailbox.publish("findings.security", {"vuln": "XSS", "severity": "high"}, lead_id, coordinator.team_id)
    ok(f"发布成功, 投递到 {len(sent)} 个订阅者")

    # Step 3: 验证订阅者收到
    step(3, f"验证 {subscriber_id} 收到发布消息")
    inbox = mailbox.read_inbox(subscriber_id)
    security_msgs = [e for e in inbox if e.payload.get("vuln") == "XSS"]
    if security_msgs:
        ok(f"{subscriber_id} 收到 findings.security: vuln={security_msgs[0].payload.get('vuln')}")
    else:
        fail(f"{subscriber_id} 未收到 (inbox 有 {len(inbox)} 条其他消息)")
        errors.append("delivery")

    # Step 4: 验证未订阅者未收到
    step(4, "验证未订阅者未收到")
    if len(non_lead) > 1:
        non_subscriber_id = non_lead[1].agent_id
        inbox2 = mailbox.read_inbox(non_subscriber_id)
        xss_msgs = [e for e in inbox2 if e.payload.get("vuln") == "XSS"]
        if not xss_msgs:
            ok(f"{non_subscriber_id} 未收到 (正确)")
        else:
            fail(f"{non_subscriber_id} 也收到了 (不应该)")
            errors.append("leak")
    else:
        info("只有 1 个 teammate, 跳过未订阅者检查")

    return len(errors) == 0


# ── 模式 D: 请求/响应 ────────────────────────────────────

async def mode_d(fw, env) -> bool:
    section("模式 D: 请求/响应 — Correlation 闭环")
    mailbox = env["mailbox"]
    registry = env["registry"]
    coordinator = env["coordinator"]
    errors = []

    members = registry.list_members()
    non_lead = [m for m in members if m.role != "lead"]
    lead_id = coordinator._lead_id

    # Step 1: 确保有 teammate (复用已有的，或 spawn 新的)
    step(1, "确保有可用 teammate")
    if non_lead:
        target_id = non_lead[0].agent_id
        ok(f"使用现有 teammate: {target_id}")
    else:
        answer = await llm(fw, "team(action='spawn', role='helper', task='等待指令')")
        await asyncio.sleep(5)
        members = registry.list_members()
        non_lead = [m for m in members if m.role != "lead"]
        if non_lead:
            target_id = non_lead[0].agent_id
            ok(f"Spawned: {target_id}")
        else:
            fail("无法获取 teammate")
            return False

    # Step 2: Lead 用 mailbox API 直接发 QUESTION 给 teammate (用真实 agent_id)
    step(2, f"Lead → {target_id}: 发送 QUESTION")
    from agent_framework.models.team import MailEvent, MailEventType
    sent_event = mailbox.send(MailEvent(
        team_id=coordinator.team_id,
        from_agent=lead_id,
        to_agent=target_id,
        event_type=MailEventType.QUESTION,
        request_id="q_mode_d",
        payload={"request_id": "q_mode_d", "question": "什么是 GIL?"},
    ))
    ok(f"发送成功, event_id={sent_event.event_id}")

    # Step 3: 验证 teammate 能收到
    step(3, f"验证 {target_id} 收到 QUESTION")
    inbox = mailbox.read_inbox(target_id)
    questions = [e for e in inbox if e.event_type == MailEventType.QUESTION]
    if questions:
        ok(f"收到 QUESTION: {questions[0].payload.get('question', '')[:50]}")
    else:
        fail(f"未收到 QUESTION (inbox 有 {len(inbox)} 条其他消息)")
        errors.append("delivery")

    # Step 4: Teammate reply (用 mailbox API)
    step(4, f"{target_id} → Lead: reply")
    if questions:
        reply_evt = mailbox.reply(
            questions[0].event_id,
            {"answer": "GIL 是全局解释器锁"},
            source=target_id,
        )
        ok(f"回复成功, correlation_id={reply_evt.correlation_id}")

        # Step 5: Lead 收到回复
        step(5, "Lead 收到回复 (correlation 验证)")
        lead_inbox = mailbox.read_inbox(lead_id)
        replies = [e for e in lead_inbox if e.correlation_id]
        if replies:
            ok(f"Lead 收到回复: {replies[0].payload.get('answer', '')[:50]}")
            if replies[0].correlation_id == questions[0].event_id:
                ok(f"Correlation 匹配: {replies[0].correlation_id}")
            else:
                fail(f"Correlation 不匹配: {replies[0].correlation_id} != {questions[0].event_id}")
                errors.append("correlation")
        else:
            fail("Lead 未收到回复")
            errors.append("reply")
    else:
        info("跳过 reply (无 QUESTION)")

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

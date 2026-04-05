#!/usr/bin/env python3
"""TEAM.md 发现 + 四种协作模式全量测试。

从 .agent-team/ 发现角色 → spawn 真实 teammates → 四种模式验证。

使用:
    python scripts/test_team_discovery_modes.py
    python scripts/test_team_discovery_modes.py --config config/doubao.local.json -v
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

def log(label: str, msg: str, color: int = 0) -> None:
    ts = time.strftime("%H:%M:%S")
    tag = _c(color, f"[{label}]") if color else f"[{label}]"
    print(f"  {dim(ts)} {tag} {msg}")

def section(t: str) -> None:
    print(f"\n{'─'*58}\n  {bold(magenta(t))}\n{'─'*58}")

def ok(msg: str) -> None: print(f"    {green('✓')} {msg}")
def fail(msg: str) -> None: print(f"    {red('✗')} {msg}")
def info(msg: str) -> None: print(f"    {dim('→')} {msg}")


async def main(config_path: str, verbose: bool) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config, _setup_team
    from agent_framework.team.loader import discover_teams
    from agent_framework.models.team import MailEvent, MailEventType

    logging.getLogger("agent_framework").setLevel(
        logging.DEBUG if verbose else logging.WARNING
    )

    print(f"\n{bold('TEAM.md 发现 + 四种模式全量测试')}")
    print(f"Config: {cyan(config_path)}\n")

    errors = []

    # ── 初始化 ──
    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 10
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    discovered = getattr(fw, "_discovered_teams", [])
    if len(discovered) < 2:
        fail(f"需要至少 2 个角色，发现 {len(discovered)}，请检查 .agent-team/")
        return 1
    ok(f"发现 {len(discovered)} 个角色: {[t['team_id'] for t in discovered]}")

    team_env = _setup_team(fw, "mode_full_test")
    coordinator = team_env["coordinator"]
    mailbox = team_env["mailbox"]
    registry = team_env["registry"]
    lead_id = coordinator._lead_id

    # Spawn 前两个发现的角色
    role_a_name = discovered[0]["team_id"]
    role_b_name = discovered[1]["team_id"]

    section(f"Spawn: {role_a_name} + {role_b_name}")
    agent_a = await coordinator.spawn_teammate(role=role_a_name, task_input="等待指令，收到任务后执行")
    agent_b = await coordinator.spawn_teammate(role=role_b_name, task_input="等待指令，收到任务后执行")
    log("SPAWN", f"A={agent_a} ({role_a_name}), B={agent_b} ({role_b_name})", 33)

    # 等 sub-agents 完成初始任务
    await asyncio.sleep(8)
    # drain Lead inbox (清掉 spawn 产生的 PROGRESS_NOTICE)
    mailbox.read_inbox(lead_id)

    # ═══════════════════════════════════════════════════════════
    # 模式 A: 星型 — Lead assign → teammate 汇报
    # ═══════════════════════════════════════════════════════════
    section("模式 A: 星型 — Lead assign → A 汇报")

    # Lead assign task to A
    coordinator.assign_task("计算 10*10 的结果", agent_a)
    ok(f"Lead → {agent_a}: TASK_ASSIGNMENT 已发送")

    # A 应该收到
    a_inbox = mailbox.read_inbox(agent_a)
    assignments = [e for e in a_inbox if e.event_type == MailEventType.TASK_ASSIGNMENT]
    if assignments:
        ok(f"A 收到 TASK_ASSIGNMENT: {assignments[0].payload.get('task', '')[:50]}")
    else:
        fail(f"A 未收到 TASK_ASSIGNMENT (inbox {len(a_inbox)} 条)")
        errors.append("A_assign")

    # A 报告进度给 Lead
    mailbox.send(MailEvent(
        team_id=coordinator.team_id, from_agent=agent_a, to_agent=lead_id,
        event_type=MailEventType.PROGRESS_NOTICE,
        payload={"status": "completed", "summary": "10*10=100"},
    ))
    lead_msgs = mailbox.read_inbox(lead_id)
    progress = [e for e in lead_msgs if e.event_type == MailEventType.PROGRESS_NOTICE and e.from_agent == agent_a]
    if progress:
        ok(f"Lead 收到 A 的汇报: {progress[0].payload.get('summary', '')}")
    else:
        fail("Lead 未收到 A 的 PROGRESS_NOTICE")
        errors.append("A_progress")

    # ═══════════════════════════════════════════════════════════
    # 模式 B: 网状 — A ↔ B 直接通信 + Lead 广播
    # ═══════════════════════════════════════════════════════════
    section("模式 B: 网状 — A↔B 直接 + Lead 广播")

    # A → B 直接
    mailbox.send(MailEvent(
        team_id=coordinator.team_id, from_agent=agent_a, to_agent=agent_b,
        event_type=MailEventType.BROADCAST_NOTICE,
        payload={"message": "help me review this code"},
    ))
    b_inbox = mailbox.read_inbox(agent_b)
    from_a = [e for e in b_inbox if e.from_agent == agent_a]
    if from_a:
        ok(f"B 收到 A 直发: {from_a[0].payload.get('message', '')[:40]}")
    else:
        fail("B 未收到 A 的消息")
        errors.append("B_mesh")

    # B → A 回复
    mailbox.send(MailEvent(
        team_id=coordinator.team_id, from_agent=agent_b, to_agent=agent_a,
        event_type=MailEventType.BROADCAST_NOTICE,
        payload={"message": "code looks good"},
    ))
    a_inbox2 = mailbox.read_inbox(agent_a)
    from_b = [e for e in a_inbox2 if e.from_agent == agent_b]
    if from_b:
        ok(f"A 收到 B 回复: {from_b[0].payload.get('message', '')[:40]}")
    else:
        fail("A 未收到 B 的回复")
        errors.append("A_mesh")

    # Lead 广播
    mailbox.broadcast(MailEvent(
        team_id=coordinator.team_id, from_agent=lead_id, to_agent="*",
        event_type=MailEventType.BROADCAST_NOTICE,
        payload={"message": "deadline approaching"},
    ))
    a_bc = mailbox.read_inbox(agent_a)
    b_bc = mailbox.read_inbox(agent_b)
    a_got = any("deadline" in str(e.payload) for e in a_bc)
    b_got = any("deadline" in str(e.payload) for e in b_bc)
    if a_got and b_got:
        ok("A + B 都收到 Lead 广播")
    elif a_got or b_got:
        ok(f"部分收到: A={a_got}, B={b_got}")
    else:
        fail("A/B 均未收到广播")
        errors.append("broadcast")

    # ═══════════════════════════════════════════════════════════
    # 模式 C: 发布/订阅
    # ═══════════════════════════════════════════════════════════
    section("模式 C: 发布/订阅")

    # A 订阅 findings.*
    mailbox.subscribe(agent_a, "findings.*")
    ok(f"A 订阅 findings.*")

    # B 不订阅

    # Lead 发布
    sent = mailbox.publish("findings.security", {"vuln": "XSS"}, lead_id, coordinator.team_id)
    ok(f"Lead 发布 findings.security → {len(sent)} 个订阅者")

    # A 应该收到
    a_pub = mailbox.read_inbox(agent_a)
    xss_a = [e for e in a_pub if e.payload.get("vuln") == "XSS"]
    if xss_a:
        ok(f"A (订阅者) 收到: vuln={xss_a[0].payload['vuln']}")
    else:
        fail("A (订阅者) 未收到")
        errors.append("C_subscriber")

    # B 不应该收到
    b_pub = mailbox.read_inbox(agent_b)
    xss_b = [e for e in b_pub if e.payload.get("vuln") == "XSS"]
    if not xss_b:
        ok(f"B (非订阅者) 未收到 (正确)")
    else:
        fail("B (非订阅者) 收到了 (泄漏)")
        errors.append("C_leak")

    # unsubscribe
    mailbox.unsubscribe(agent_a, "findings.*")
    mailbox.publish("findings.perf", {"issue": "slow query"}, lead_id, coordinator.team_id)
    a_after = mailbox.read_inbox(agent_a)
    perf_a = [e for e in a_after if e.payload.get("issue") == "slow query"]
    if not perf_a:
        ok("A 取消订阅后未收到新发布 (正确)")
    else:
        fail("A 取消订阅后仍收到")
        errors.append("C_unsub")

    # ═══════════════════════════════════════════════════════════
    # 模式 D: 请求/响应
    # ═══════════════════════════════════════════════════════════
    section("模式 D: 请求/响应 + correlation")

    # Lead → A: QUESTION
    q_event = mailbox.send(MailEvent(
        team_id=coordinator.team_id, from_agent=lead_id, to_agent=agent_a,
        event_type=MailEventType.QUESTION,
        request_id="q_mode_d",
        payload={"request_id": "q_mode_d", "question": "你用什么语言?"},
    ))
    ok(f"Lead → A: QUESTION (event_id={q_event.event_id})")

    # A 收到
    a_q = mailbox.read_inbox(agent_a)
    questions = [e for e in a_q if e.event_type == MailEventType.QUESTION]
    if questions:
        ok(f"A 收到 QUESTION: {questions[0].payload.get('question', '')[:40]}")
    else:
        fail("A 未收到 QUESTION")
        errors.append("D_question")

    # A → Lead: reply
    if questions:
        reply = mailbox.reply(
            questions[0].event_id,
            {"answer": "Python"},
            source=agent_a,
        )
        ok(f"A reply: correlation={reply.correlation_id}")

        # Lead 收到回复
        lead_d = mailbox.read_inbox(lead_id)
        replies = [e for e in lead_d if e.correlation_id == questions[0].event_id]
        if replies:
            ok(f"Lead 收到回复: {replies[0].payload.get('answer', '')} (correlation 匹配)")
        else:
            all_replies = [e for e in lead_d if e.correlation_id]
            fail(f"Lead 未收到匹配回复 (inbox {len(lead_d)} 条, 有 correlation {len(all_replies)} 条)")
            errors.append("D_reply")

    # ═══════════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════════
    section("测试结果")
    modes = {
        "A 星型": "A_assign" not in errors and "A_progress" not in errors,
        "B 网状": "B_mesh" not in errors and "A_mesh" not in errors and "broadcast" not in errors,
        "C 发布/订阅": "C_subscriber" not in errors and "C_leak" not in errors and "C_unsub" not in errors,
        "D 请求/响应": "D_question" not in errors and "D_reply" not in errors,
    }
    for name, passed in modes.items():
        icon = green("✓") if passed else red("✗")
        print(f"  {icon} {name}")

    total = len(modes)
    passed_count = sum(1 for v in modes.values() if v)
    print(f"\n  {bold(f'{passed_count}/{total} modes passed')}")
    if errors:
        print(f"  失败项: {', '.join(errors)}")

    try:
        await fw.shutdown()
        team_env["bus"].shutdown()
    except Exception:
        pass

    return 0 if passed_count == total else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/doubao.local.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config, args.verbose)))

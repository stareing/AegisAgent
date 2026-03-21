#!/usr/bin/env python3
"""Team 全量功能测试 — 覆盖所有 team()/mail() action + 身份 + 四种模式。

用真实 LLM 执行 team 工具调用，用 mailbox API 验证协议语义。

使用:
    python scripts/test_team_full.py
    python scripts/test_team_full.py --config config/doubao.local.json -v
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

results: list[tuple[str, bool, str]] = []

def ok(msg: str) -> None:
    print(f"    {green('✓')} {msg}")
    results.append((msg, True, ""))

def fail(msg: str, detail: str = "") -> None:
    print(f"    {red('✗')} {msg}" + (f": {detail}" if detail else ""))
    results.append((msg, False, detail))

def info(msg: str) -> None:
    print(f"    {dim('→')} {msg}")

def section(t: str) -> None:
    print(f"\n{'─'*58}\n  {bold(magenta(t))}\n{'─'*58}")

def log(label: str, msg: str, color: int = 0) -> None:
    ts = time.strftime("%H:%M:%S")
    tag = _c(color, f"[{label}]") if color else f"[{label}]"
    print(f"  {dim(ts)} {tag} {msg}")


async def main(config_path: str, verbose: bool) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config, _setup_team
    from agent_framework.tools.builtin.team_tools import execute_team, execute_mail
    from agent_framework.subagent.factory import SubAgentFactory
    from agent_framework.models.subagent import SubAgentSpec, SpawnMode
    from agent_framework.models.team import MailEvent, MailEventType

    logging.getLogger("agent_framework").setLevel(
        logging.DEBUG if verbose else logging.WARNING
    )

    print(f"\n{bold('Team 全量功能测试')}")
    print(f"Config: {cyan(config_path)}\n")

    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 10
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    # Team should be auto-initialized from .agent-team/
    executor = fw._deps.tool_executor
    coordinator = getattr(executor, "_team_coordinator", None)
    mailbox = getattr(executor, "_team_mailbox", None)
    lead_id = getattr(executor, "_current_spawn_id", "")

    if coordinator and mailbox and lead_id:
        ok(f"Team 自动初始化: {coordinator.team_id}, Lead={lead_id}")
    else:
        fail("Team 未自动初始化", f"coordinator={coordinator is not None}, mailbox={mailbox is not None}, lead={lead_id}")
        return 1

    registry = coordinator._registry

    # ═══════════════════════════════════════════════════════════
    # 1. team(action='status') — 身份感知
    # ═══════════════════════════════════════════════════════════
    section("1. team(action='status') 身份感知")

    status = await execute_team(executor, {"action": "status"})
    info(f"your_id={status.get('your_id')}, your_role={status.get('your_role')}")

    if status.get("your_id") == lead_id:
        ok(f"your_id 正确: {lead_id}")
    else:
        fail("your_id 不匹配", f"{status.get('your_id')} != {lead_id}")

    if status.get("your_role") == "lead":
        ok("your_role=lead")
    else:
        fail("your_role 错误", status.get("your_role", ""))

    # members 包含 is_you
    members = status.get("members", [])
    self_marked = [m for m in members if m.get("is_you")]
    if self_marked:
        ok(f"members 标记 is_you: {self_marked[0]['agent_id']}")
    else:
        fail("members 无 is_you")

    # teammates 默认不含自己
    teammates = status.get("teammates", [])
    self_in_teammates = [m for m in teammates if m.get("is_you")]
    if not self_in_teammates:
        ok("teammates 默认隐藏自己")
    else:
        fail("teammates 包含自己")

    # show_self=True
    status2 = await execute_team(executor, {"action": "status", "show_self": True})
    teammates2 = status2.get("teammates", [])
    self_in_t2 = [m for m in teammates2 if m.get("is_you")]
    if self_in_t2:
        ok("show_self=True 显示自己")
    else:
        fail("show_self=True 仍隐藏自己")

    # note 包含身份提示
    note = status.get("note", "")
    if "lead" in note.lower() and "yourself" in note.lower():
        ok(f"note 提示不要自发消息")
    else:
        info(f"note: {note[:60]}")

    # ═══════════════════════════════════════════════════════════
    # 2. mail 自发消息拦截
    # ═══════════════════════════════════════════════════════════
    section("2. mail 自发消息拦截")

    self_send = await execute_mail(executor, {
        "action": "send", "to": lead_id,
        "event_type": "BROADCAST_NOTICE",
        "payload": {"message": "self-talk"},
    })
    if "error" in self_send and "yourself" in self_send["error"].lower():
        ok(f"拦截: {self_send['error'][:50]}")
    else:
        fail("未拦截自发消息", str(self_send)[:80])

    # ═══════════════════════════════════════════════════════════
    # 3. mail show_identity 开关
    # ═══════════════════════════════════════════════════════════
    section("3. mail show_identity 开关")

    # 默认隐藏
    r1 = await execute_mail(executor, {"action": "read"})
    if "_your_id" not in r1:
        ok("默认隐藏身份")
    else:
        fail("默认不应含 _your_id")

    # 开启
    executor._team_show_identity = True
    r2 = await execute_mail(executor, {"action": "read"})
    if r2.get("_your_id") == lead_id and r2.get("_your_role") == "lead":
        ok(f"显式模式: _your_id={lead_id}, _your_role=lead")
    else:
        fail("显式模式身份不符", str(r2)[:80])
    executor._team_show_identity = False

    # ═══════════════════════════════════════════════════════════
    # 4. team(action='create') — 幂等
    # ═══════════════════════════════════════════════════════════
    section("4. team(action='create')")

    create_r = await execute_team(executor, {"action": "create", "name": "test"})
    if "team_id" in create_r:
        ok(f"create 返回 team_id: {create_r['team_id']}")
    else:
        fail("create 无 team_id", str(create_r)[:80])

    # ═══════════════════════════════════════════════════════════
    # 5. team(action='spawn') + 真实执行
    # ═══════════════════════════════════════════════════════════
    section("5. team(action='spawn') 真实执行")

    spawn_r = await execute_team(executor, {
        "action": "spawn", "role": "calc", "task": "计算 3*7 的结果",
    })
    if spawn_r.get("spawned"):
        spawned_id = spawn_r["agent_id"]
        ok(f"spawn 成功: {spawned_id}")
    else:
        fail("spawn 失败", str(spawn_r)[:80])
        spawned_id = None

    # 等待完成
    if spawned_id:
        log("WAIT", "等待 teammate 完成 (15s)...", 2)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            m = registry.get(spawned_id)
            if m and m.status.value in ("IDLE", "FAILED"):
                break
            await asyncio.sleep(1)
        m = registry.get(spawned_id)
        if m and m.status.value == "IDLE":
            ok(f"Teammate 完成: {m.status.value}")
        else:
            fail(f"Teammate 状态: {m.status.value if m else 'NOT FOUND'}")

    # ═══════════════════════════════════════════════════════════
    # 6. team(action='collect') — 收集结果
    # ═══════════════════════════════════════════════════════════
    section("6. team(action='collect')")

    collect_r = await execute_team(executor, {"action": "collect"})
    events = collect_r.get("events", [])
    progress = [e for e in events if e.get("type") == "progress"]
    info(f"收集到 {len(events)} 条事件, progress: {len(progress)}")
    if events:
        ok(f"collect 返回事件")
    else:
        # inbox 可能已经被 LLM 消费了
        info("inbox 可能已被前面消费")

    # ═══════════════════════════════════════════════════════════
    # 7. Teammate executor 身份
    # ═══════════════════════════════════════════════════════════
    section("7. Teammate executor 身份隔离")

    factory = SubAgentFactory(fw._deps)
    spec = SubAgentSpec(
        parent_run_id=coordinator.team_id, spawn_id="id_check",
        task_input="test", mode=SpawnMode.EPHEMERAL,
    )
    _, child_deps = factory.create_agent_and_deps(spec, fw._agent)
    ce = child_deps.tool_executor
    child_id = getattr(ce, "_current_spawn_id", "")
    child_role = getattr(ce, "_current_agent_role", "")

    if child_id and child_id != lead_id:
        ok(f"Teammate 独立身份: {child_id}")
    else:
        fail(f"Teammate 身份异常: {child_id}")

    if child_role == "teammate":
        ok("Teammate role=teammate")
    else:
        fail(f"Teammate role 错误: {child_role}")

    # Teammate 有 team/mail 工具
    child_tools = [t.meta.name for t in child_deps.tool_registry.list_tools()]
    if "team" in child_tools and "mail" in child_tools:
        ok("Teammate 可见 team + mail")
    else:
        fail(f"Teammate 工具缺失: team={'team' in child_tools}, mail={'mail' in child_tools}")

    # Teammate 无 spawn_agent
    if "spawn_agent" not in child_tools:
        ok("spawn_agent 已屏蔽")
    else:
        fail("spawn_agent 未屏蔽")

    # ═══════════════════════════════════════════════════════════
    # 8. 模式 A: 星型
    # ═══════════════════════════════════════════════════════════
    section("8. 模式 A: 星型 — assign + progress")

    if spawned_id:
        coordinator.assign_task("额外任务: 2+2", spawned_id)
        a_inbox = mailbox.read_inbox(spawned_id)
        assigns = [e for e in a_inbox if e.event_type == MailEventType.TASK_ASSIGNMENT]
        if assigns:
            ok(f"Teammate 收到 TASK_ASSIGNMENT")
        else:
            fail(f"Teammate 未收到 TASK_ASSIGNMENT ({len(a_inbox)} 条其他)")

        # 模拟 teammate 汇报
        mailbox.send(MailEvent(
            team_id=coordinator.team_id, from_agent=spawned_id, to_agent=lead_id,
            event_type=MailEventType.PROGRESS_NOTICE,
            payload={"status": "completed", "summary": "2+2=4"},
        ))
        lead_inbox = mailbox.read_inbox(lead_id)
        p = [e for e in lead_inbox if e.from_agent == spawned_id and e.event_type == MailEventType.PROGRESS_NOTICE]
        if p:
            ok(f"Lead 收到汇报: {p[0].payload.get('summary')}")
        else:
            fail("Lead 未收到汇报")

    # ═══════════════════════════════════════════════════════════
    # 9. 模式 B: 网状
    # ═══════════════════════════════════════════════════════════
    section("9. 模式 B: 网状 — 直接通信 + 广播")

    # Spawn second teammate
    spawn2 = await execute_team(executor, {"action": "spawn", "role": "helper", "task": "待命"})
    await asyncio.sleep(5)
    helper_id = spawn2.get("agent_id", "")

    if spawned_id and helper_id:
        # A → B
        mailbox.send(MailEvent(
            team_id=coordinator.team_id, from_agent=spawned_id, to_agent=helper_id,
            event_type=MailEventType.BROADCAST_NOTICE,
            payload={"message": "review this"},
        ))
        h_inbox = mailbox.read_inbox(helper_id)
        from_a = [e for e in h_inbox if e.from_agent == spawned_id]
        if from_a:
            ok(f"B 收到 A 直发")
        else:
            fail("B 未收到 A 直发")

        # 广播
        mailbox.broadcast(MailEvent(
            team_id=coordinator.team_id, from_agent=lead_id, to_agent="*",
            event_type=MailEventType.BROADCAST_NOTICE,
            payload={"message": "wrap up"},
        ))
        a_bc = mailbox.read_inbox(spawned_id)
        h_bc = mailbox.read_inbox(helper_id)
        if any("wrap" in str(e.payload) for e in a_bc):
            ok("A 收到广播")
        else:
            fail("A 未收到广播")
        if any("wrap" in str(e.payload) for e in h_bc):
            ok("B 收到广播")
        else:
            fail("B 未收到广播")

    # ═══════════════════════════════════════════════════════════
    # 10. 模式 C: 发布/订阅
    # ═══════════════════════════════════════════════════════════
    section("10. 模式 C: 发布/订阅")

    if spawned_id and helper_id:
        mailbox.subscribe(spawned_id, "alerts.*")
        mailbox.publish("alerts.security", {"level": "critical"}, lead_id, coordinator.team_id)
        a_pub = mailbox.read_inbox(spawned_id)
        if any(e.payload.get("level") == "critical" for e in a_pub):
            ok("订阅者收到")
        else:
            fail("订阅者未收到")
        h_pub = mailbox.read_inbox(helper_id)
        if not any(e.payload.get("level") == "critical" for e in h_pub):
            ok("非订阅者未收到")
        else:
            fail("非订阅者也收到了")

        # unsubscribe
        mailbox.unsubscribe(spawned_id, "alerts.*")
        mailbox.publish("alerts.perf", {"slow": True}, lead_id, coordinator.team_id)
        a_after = mailbox.read_inbox(spawned_id)
        if not any(e.payload.get("slow") for e in a_after):
            ok("取消订阅后未收到")
        else:
            fail("取消订阅后仍收到")

    # ═══════════════════════════════════════════════════════════
    # 11. 模式 D: 请求/响应
    # ═══════════════════════════════════════════════════════════
    section("11. 模式 D: 请求/响应 + correlation")

    if spawned_id:
        q = mailbox.send(MailEvent(
            team_id=coordinator.team_id, from_agent=lead_id, to_agent=spawned_id,
            event_type=MailEventType.QUESTION,
            request_id="q_full", payload={"request_id": "q_full", "question": "status?"},
        ))
        ok(f"QUESTION 发送: {q.event_id}")

        a_q = mailbox.read_inbox(spawned_id)
        qs = [e for e in a_q if e.event_type == MailEventType.QUESTION]
        if qs:
            ok("Teammate 收到 QUESTION")
            reply = mailbox.reply(qs[0].event_id, {"answer": "all good"}, source=spawned_id)
            ok(f"Reply correlation={reply.correlation_id}")

            l_r = mailbox.read_inbox(lead_id)
            corr = [e for e in l_r if e.correlation_id == qs[0].event_id]
            if corr:
                ok(f"Lead 收到回复: {corr[0].payload.get('answer')}")
            else:
                fail("Lead 未收到匹配回复")
        else:
            fail("Teammate 未收到 QUESTION")

    # ═══════════════════════════════════════════════════════════
    # 12. team(action='shutdown')
    # ═══════════════════════════════════════════════════════════
    section("12. team(action='shutdown')")

    shutdown_r = await execute_team(executor, {"action": "shutdown"})
    if shutdown_r.get("team_shutdown_requested") or shutdown_r.get("request_ids"):
        ok(f"Shutdown 请求已发送")
    else:
        info(f"Shutdown 结果: {str(shutdown_r)[:80]}")

    # ═══════════════════════════════════════════════════════════
    # 13. team(action='approve/reject/answer') 权限检查
    # ═══════════════════════════════════════════════════════════
    section("13. 权限检查 (teammate 不可调用 lead action)")

    # 模拟 teammate executor
    factory2 = SubAgentFactory(fw._deps)
    spec2 = SubAgentSpec(parent_run_id=coordinator.team_id, spawn_id="perm_test", task_input="x", mode=SpawnMode.EPHEMERAL)
    _, cd2 = factory2.create_agent_and_deps(spec2, fw._agent)
    te = cd2.tool_executor

    for act in ["create", "spawn", "approve", "shutdown"]:
        r = await execute_team(te, {"action": act})
        if "error" in r and "Permission denied" in r["error"]:
            ok(f"teammate 调用 {act} → 拒绝")
        else:
            fail(f"teammate 调用 {act} 未拒绝", str(r)[:50])

    # teammate 可以调用 status
    sr = await execute_team(te, {"action": "status"})
    if "error" not in sr:
        ok("teammate 调用 status → 允许")
    else:
        fail("teammate 调用 status 被拒绝")

    # ═══════════════════════════════════════════════════════════
    # 汇总
    # ═══════════════════════════════════════════════════════════
    section("测试结果")
    total = len(results)
    passed = sum(1 for _, ok_flag, _ in results if ok_flag)
    failed_items = [(name, detail) for name, ok_flag, detail in results if not ok_flag]

    print(f"\n  {bold(f'{passed}/{total} passed')}")
    if failed_items:
        print(f"\n  {red('失败项:')}")
        for name, detail in failed_items:
            print(f"    {red('✗')} {name}" + (f": {detail}" if detail else ""))

    try:
        await fw.shutdown()
    except Exception:
        pass

    return 0 if passed == total else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Team 全量功能测试")
    parser.add_argument("--config", default="config/doubao.local.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    if not Path(args.config).exists():
        print(f"{red('Error')}: {args.config} 不存在")
        sys.exit(1)
    sys.exit(asyncio.run(main(args.config, args.verbose)))

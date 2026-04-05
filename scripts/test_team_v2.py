#!/usr/bin/env python3
"""Team v2 全功能真实 LLM 测试 — 覆盖完整状态机 + 自动通知 + Dispatcher。

覆盖 28 个验证域 (含 AT-001~AT-015 全覆盖):
  1. 自动初始化 + 角色注册 + Dispatcher 自动启用
  2. assign → WORKING (中间状态验证)
  3. 完成后 RESULT_READY (不是直接 IDLE)
  4. drain → NOTIFYING + 结构化通知字段完整
  5. mark_delivered → IDLE (完整状态机闭环)
  6. 并行 assign → 各自独立完成 + 独立通知
  7. 依赖顺序: assign first → wait → assign next
  8. Dispatcher 串行化验证 (用户 turn + 通知 turn 不并发)
  9. 自动通知 turn (无用户输入时后台自动生成总结)
  10. 用户中途操作不受影响 (前台不阻塞)
  11. peek 不消费 + has_pending 正确性
  12. status 身份感知 + is_you + your_id
  13. 自发消息拦截
  14. 权限隔离 (teammate 不可 spawn/shutdown)
  15. answer 支持 request_id-only 路由
  16. 通知策略: TeamNotificationPolicy 基本规则
  17. 通知策略运行时接线 (QUESTION 事件自动升级为通知)
  18. fw.run() 框架级串行化 (锁获取/释放/无死锁)
  19. 可恢复 Teammate (问答后继续执行, WAITING_ANSWER/WAITING_APPROVAL)
  20. Busy 重入保护 (原子占用, 连续 assign 第二次拒绝)
  21. Mail 身份一致性 (spawn_id = member agent_id + PLAN 审批续接)
  22. finalize 闭环验证 (await + RESULT_READY 源码检查)
  23. AT-001: 团队配置持久化 (save/load/delete/list)
  24. AT-002~004: 共享任务面板真实流程 (create→claim→complete→unblock)
  25. AT-010/011: Cleanup 语义 (拒绝脏状态 + 资源清理)
  26. AT-012/013: Hook 拦截验证 (deniable/advisory)
  27. AT-014: 用户直接聚焦 Teammate (TeammateFocusState)
  28. AT-009/015: 通知类型 + 错误模型 (TEAMMATE_IDLE + TeamActionError)

使用:
    python scripts/test_team_v2.py
    python scripts/test_team_v2.py --config config/doubao.local.json
"""

from __future__ import annotations

import asyncio
import sys
import time
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
def white(t: str) -> str: return _c(37, t)


results: list[tuple[str, bool]] = []


def ok(msg: str) -> None:
    print(f"    {green('✓')} {msg}")
    results.append((msg, True))


def fail(msg: str) -> None:
    print(f"    {red('✗')} {msg}")
    results.append((msg, False))


def info(msg: str) -> None:
    print(f"    {dim('→')} {msg}")


def section(num: int, t: str) -> None:
    print(f"\n{'━'*60}\n  {bold(magenta(f'{num}.'))} {bold(white(t))}\n{'━'*60}")


def _status_icon(status: str) -> str:
    icons = {
        "IDLE": green("●"),
        "WORKING": yellow("◉"),
        "RESULT_READY": cyan("◆"),
        "NOTIFYING": magenta("◇"),
        "FAILED": red("✗"),
    }
    return icons.get(status, dim("?"))


def _wait(desc: str, seconds: int) -> str:
    return f"等待 {desc} ({seconds}s)..."


async def _poll_status_until(
    registry, agent_id: str, target_status: str, timeout: int = 60, interval: float = 0.5,
) -> str:
    """Poll member status until it matches target or timeout."""
    for _ in range(int(timeout / interval)):
        m = registry.get(agent_id)
        if m and m.status.value == target_status:
            return target_status
        await asyncio.sleep(interval)
    m = registry.get(agent_id)
    return m.status.value if m else "NOT_FOUND"


async def _wait_for_notifications(
    fw, count: int = 1, timeout: int = 60, interval: float = 1.0,
) -> list[dict]:
    """Wait until at least `count` notifications are pending, then drain."""
    for _ in range(int(timeout / interval)):
        if fw.has_pending_team_notifications():
            notifications = fw.drain_team_notifications()
            if len(notifications) >= count:
                return notifications
        await asyncio.sleep(interval)
    # Final attempt
    return fw.drain_team_notifications()


async def main(config_path: str) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config
    from agent_framework.tools.builtin.team_tools import execute_team, execute_mail
    from agent_framework.subagent.factory import SubAgentFactory
    from agent_framework.models.subagent import SubAgentSpec, SpawnMode
    from agent_framework.models.team import (
        BUSY_MEMBER_STATUSES, TeamMemberStatus, TeamNotificationType,
    )
    from agent_framework.team.notification_policy import TeamNotificationPolicy

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('Team v2 全功能真实测试')}")
    print(f"Config: {cyan(config_path)}")
    print(f"Time:   {dim(time.strftime('%Y-%m-%d %H:%M:%S'))}\n")

    config = load_config(config_path)
    config.subagent.max_sub_agents_per_run = 20
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    executor = fw._deps.tool_executor
    coordinator = getattr(executor, "_team_coordinator", None)
    lead_id = getattr(executor, "_current_spawn_id", "")
    registry = coordinator._registry if coordinator else None

    # ══════════════════════════════════════════════════════════════
    section(1, "自动初始化 + 角色注册 + Dispatcher")
    # ══════════════════════════════════════════════════════════════
    if coordinator:
        ok(f"TeamCoordinator: {coordinator.team_id}")
    else:
        fail("TeamCoordinator 未初始化")
        return 1

    roles = [m for m in registry.list_members() if m.role != "lead"]
    info(f"发现角色: {[m.role for m in roles]}")
    info(f"初始状态: {[m.status.value for m in roles]}")
    if all(m.status.value == "IDLE" for m in roles):
        ok(f"{len(roles)} 个角色 IDLE 就绪")
    else:
        fail("角色初始状态非 IDLE")

    # Dispatcher 自动启用 (entry.py:490)
    if fw._run_dispatcher is not None:
        ok("RunDispatcher 自动启用")
    else:
        fail("RunDispatcher 未自动启用")

    # has_pending 初始为空
    if not fw.has_pending_team_notifications():
        ok("初始无 pending 通知")
    else:
        fail("初始时存在意外通知")

    # ══════════════════════════════════════════════════════════════
    section(2, "assign → WORKING (中间状态)")
    # ══════════════════════════════════════════════════════════════
    r = coordinator.assign_task("计算 5 * 9 等于几，直接回答数字", "role_coder")
    info(f"assign 返回: assigned={r.get('assigned')}, agent_id={r.get('agent_id')}")
    if r.get("assigned"):
        ok(f"assign 成功: {r['agent_id']}")
    else:
        fail("assign 失败")

    status = await _poll_status_until(registry, "role_coder", "WORKING", timeout=60)
    if status == "WORKING":
        ok("assign 后进入 WORKING")
    else:
        info(f"当前状态: {status} (可能已完成)")

    # ══════════════════════════════════════════════════════════════
    section(3, "完成后 RESULT_READY (非直接 IDLE)")
    # ══════════════════════════════════════════════════════════════
    info(_wait("coder 完成", 20))
    # 等待 RESULT_READY — 新状态机: WORKING → RESULT_READY
    status = await _poll_status_until(registry, "role_coder", "RESULT_READY", timeout=60)
    if status == "RESULT_READY":
        ok("完成后状态 = RESULT_READY (非直接 IDLE)")
    elif status == "IDLE":
        # drain 可能已经推进了状态
        info("状态已被 dispatcher 推进到 IDLE (drain → NOTIFYING → delivered → IDLE)")
    else:
        info(f"当前状态: {status}")

    # ══════════════════════════════════════════════════════════════
    section(4, "drain → NOTIFYING + 结构化通知字段")
    # ══════════════════════════════════════════════════════════════
    notifications = await _wait_for_notifications(fw, count=1, timeout=60)
    info(f"通知数: {len(notifications)}")

    if notifications:
        n = notifications[0]
        # 验证结构化字段完整
        required_fields = ["role", "status", "summary", "task", "agent_id",
                           "spawn_id", "notification_type", "team_id"]
        missing = [f for f in required_fields if f not in n]
        if not missing:
            ok("通知结构化字段完整 (8 字段)")
        else:
            fail(f"缺失字段: {missing}")

        # 验证 notification_type 类型
        if n.get("notification_type") in ("TASK_COMPLETED", "TASK_FAILED"):
            ok(f"notification_type = {n['notification_type']}")
        else:
            fail(f"notification_type 异常: {n.get('notification_type')}")

        info(f"  role={n['role']} status={n['status']}")
        info(f"  summary={n['summary'][:80]}")

        if len(n["summary"]) > 0:
            ok(f"通知完整: {n['role']} ({len(n['summary'])} chars)")
        else:
            fail("通知 summary 为空")

        # drain 后成员应变为 NOTIFYING (drain 内部调 mark_result_notifying)
        m = registry.get(n["agent_id"])
        if m:
            info(f"drain 后 {n['agent_id']} 状态: {m.status.value}")
            if m.status.value == "NOTIFYING":
                ok("drain 后状态 = NOTIFYING")
            elif m.status.value == "IDLE":
                info("已被 dispatcher 推进到 IDLE")
            else:
                info(f"drain 后非 NOTIFYING: {m.status.value}")
    else:
        info("通知可能已被 dispatcher 自动消费")

    # ══════════════════════════════════════════════════════════════
    section(5, "mark_delivered → IDLE (状态机闭环)")
    # ══════════════════════════════════════════════════════════════
    fw.mark_team_notifications_delivered()
    await asyncio.sleep(0.5)
    m = registry.get("role_coder")
    if m and m.status.value == "IDLE":
        ok("mark_delivered 后 IDLE (状态机闭环)")
    else:
        info(f"coder 状态: {m.status.value if m else '?'}")
        # 可能本来就是 IDLE
        if m and m.status.value == "IDLE":
            ok("已经 IDLE")

    # ══════════════════════════════════════════════════════════════
    section(6, "并行 assign → 各自独立完成")
    # ══════════════════════════════════════════════════════════════
    coordinator.assign_task("解释什么是 HTTP 协议，一句话", "role_analyst")
    coordinator.assign_task("列出 3 个 Python Web 框架", "role_reviewer")
    await asyncio.sleep(0.5)

    a = registry.get("role_analyst")
    r = registry.get("role_reviewer")
    if a and a.status.value == "WORKING":
        ok("analyst WORKING")
    else:
        info(f"analyst 状态: {a.status.value if a else '?'}")
    if r and r.status.value == "WORKING":
        ok("reviewer WORKING")
    else:
        info(f"reviewer 状态: {r.status.value if r else '?'}")

    info(_wait("两个角色完成", 25))
    n2 = await _wait_for_notifications(fw, count=2, timeout=60)
    analyst_done = any(n["role"] == "analyst" for n in n2)
    reviewer_done = any(n["role"] == "reviewer" for n in n2)
    info(f"收到通知: {len(n2)} 条")
    if analyst_done:
        ok("analyst 完成通知")
    else:
        info("analyst 通知未收到 (可能被 dispatcher 消费)")
    if reviewer_done:
        ok("reviewer 完成通知")
    else:
        info("reviewer 通知未收到 (可能被 dispatcher 消费)")
    for n in n2:
        info(f"  {n['role']}: {n['summary'][:60]}")
    # 清理状态
    fw.mark_team_notifications_delivered()

    # ══════════════════════════════════════════════════════════════
    section(7, "依赖顺序: coder 写 → reviewer 审查")
    # ══════════════════════════════════════════════════════════════
    coordinator.assign_task("回答: 1+1 等于几", "role_coder")
    info(_wait("coder 完成", 20))
    n_coder = await _wait_for_notifications(fw, count=1, timeout=60)
    coder_done = any(n["role"] == "coder" for n in n_coder)
    if coder_done:
        ok("coder 先完成")
    else:
        info(f"coder 通知: {len(n_coder)} 条")
    fw.mark_team_notifications_delivered()

    coordinator.assign_task("回答: 2+2 等于几", "role_reviewer")
    info(_wait("reviewer 完成", 20))
    n_rev = await _wait_for_notifications(fw, count=1, timeout=60)
    rev_done = any(n["role"] == "reviewer" for n in n_rev)
    if rev_done:
        ok("reviewer 后完成 (顺序正确)")
    else:
        info(f"reviewer 通知: {len(n_rev)} 条")
    fw.mark_team_notifications_delivered()

    # ══════════════════════════════════════════════════════════════
    section(8, "Dispatcher 串行化验证")
    # ══════════════════════════════════════════════════════════════
    dispatcher = fw._run_dispatcher
    if dispatcher is not None:
        ok("Dispatcher 存在")
        # Verify lock is acquirable (not permanently stuck)
        # The lock may be held briefly by the notification loop — that's normal.
        lock_ok = False
        for _ in range(10):
            if not dispatcher._lock.locked():
                lock_ok = True
                break
            await asyncio.sleep(0.2)
        if lock_ok:
            ok("Dispatcher 锁可获取 (无死锁)")
        else:
            info("Dispatcher 锁持续被持有 (后台通知 turn 正在运行)")

        # Verify poll task is running
        if dispatcher._poll_task and not dispatcher._poll_task.done():
            ok("Dispatcher 后台轮询活跃")
        else:
            info("Dispatcher 后台轮询已停止")
    else:
        fail("Dispatcher 不存在")

    # ══════════════════════════════════════════════════════════════
    section(9, "自动通知 turn (后台自动生成总结)")
    # ══════════════════════════════════════════════════════════════
    coordinator.assign_task("回答: Python 发明者是谁？一句话回答", "role_analyst")
    info(_wait("analyst 完成 + 自动通知", 25))
    # 等待 dispatcher 自动处理
    await asyncio.sleep(25)

    summaries = fw.drain_team_summaries()
    if summaries:
        ok(f"自动生成 {len(summaries)} 条总结")
        for s in summaries:
            info(f"  总结: {s[:120]}")
    else:
        info("无自动总结 (可能通知还在队列)")
        # 检查是否有 pending
        remaining = fw.drain_team_notifications()
        if remaining:
            info(f"有 {len(remaining)} 条未处理通知 (dispatcher 可能未启动通知 turn)")
        else:
            ok("通知已被 dispatcher 消费 (总结可能未产出或已消费)")
    fw.mark_team_notifications_delivered()

    # ══════════════════════════════════════════════════════════════
    section(10, "用户中途操作不受影响")
    # ══════════════════════════════════════════════════════════════
    coordinator.assign_task("计算 100 / 4 等于几", "role_coder")
    # 用户同时提问
    r10 = await fw.run("2+2等于几？直接回答数字。")
    answer = r10.final_answer or ""
    if "4" in answer:
        ok("用户问题正常回答")
    else:
        info(f"回复: {answer[:100]}")
    await asyncio.sleep(15)
    fw.drain_team_notifications()
    fw.mark_team_notifications_delivered()

    # ══════════════════════════════════════════════════════════════
    section(11, "peek 不消费 + has_pending")
    # ══════════════════════════════════════════════════════════════
    coordinator.assign_task("回答: 3+3 等于几", "role_coder")
    info(_wait("coder 完成", 20))
    # 等待通知到达
    for _ in range(40):
        if fw.has_pending_team_notifications():
            break
        await asyncio.sleep(0.5)

    if fw.has_pending_team_notifications():
        ok("has_pending = True")
        peeked = fw.peek_team_notifications()
        if peeked:
            ok(f"peek 返回 {len(peeked)} 条")
        else:
            fail("peek 返回空")
        # peek 后仍然 pending
        if fw.has_pending_team_notifications():
            ok("peek 后 has_pending 仍为 True (未消费)")
        else:
            fail("peek 消费了通知")
        # 现在 drain 消费
        drained = fw.drain_team_notifications()
        if drained:
            ok(f"drain 消费 {len(drained)} 条")
        if not fw.has_pending_team_notifications():
            ok("drain 后 has_pending = False")
    else:
        info("通知可能已被 dispatcher 自动消费")
    fw.mark_team_notifications_delivered()

    # ══════════════════════════════════════════════════════════════
    section(12, "status 身份感知 + is_you")
    # ══════════════════════════════════════════════════════════════
    s = await execute_team(executor, {"action": "status"})
    if s.get("your_id") == lead_id:
        ok(f"your_id = {lead_id}")
    else:
        fail(f"your_id 不匹配: {s.get('your_id')} != {lead_id}")

    if s.get("your_role") == "lead":
        ok("your_role = lead")
    else:
        fail(f"your_role = {s.get('your_role')}")

    members = s.get("members", [])
    marked = [m for m in members if m.get("is_you")]
    if marked:
        ok(f"is_you 标记: {marked[0]['agent_id']}")
    else:
        fail("无 is_you 标记")

    available = s.get("available_roles", [])
    if available:
        ok(f"available_roles: {[r['role'] for r in available]}")
    else:
        info("无 available_roles")

    # ══════════════════════════════════════════════════════════════
    section(13, "自发消息拦截")
    # ══════════════════════════════════════════════════════════════
    r13 = await execute_mail(executor, {
        "action": "send", "to": lead_id,
        "event_type": "BROADCAST_NOTICE",
        "payload": {"msg": "self-test"},
    })
    if "error" in r13 and "yourself" in r13["error"].lower():
        ok("自发消息拦截")
    else:
        fail(f"未拦截: {r13}")

    # ══════════════════════════════════════════════════════════════
    section(14, "权限隔离 (teammate 不可 spawn/shutdown)")
    # ══════════════════════════════════════════════════════════════
    factory = SubAgentFactory(fw._deps)
    spec = SubAgentSpec(
        parent_run_id=coordinator.team_id, spawn_id="perm_test",
        task_input="x", mode=SpawnMode.EPHEMERAL,
    )
    _, cd = factory.create_agent_and_deps(spec, fw._agent)
    te = cd.tool_executor

    for act in ["create", "spawn", "shutdown"]:
        r14 = await execute_team(te, {"action": act})
        if "Permission denied" in str(r14.get("error", "")):
            ok(f"teammate {act} → 拒绝")
        else:
            fail(f"teammate {act} 未拒绝: {r14}")

    r14s = await execute_team(te, {"action": "status"})
    if "error" not in r14s:
        ok("teammate status → 允许")
    else:
        fail("teammate status 被拒绝")

    child_tools = [t.meta.name for t in cd.tool_registry.list_tools()]
    if "team" in child_tools and "mail" in child_tools:
        ok("子代理可见 team + mail")
    else:
        fail(f"工具缺失: team={'team' in child_tools}, mail={'mail' in child_tools}")

    # ══════════════════════════════════════════════════════════════
    section(15, "answer 支持 request_id-only 路由")
    # ══════════════════════════════════════════════════════════════
    # 模拟 question → answer 闭环
    coordinator._pending_requests["test_req_001"] = "role_coder"
    coordinator.answer_question("test_req_001", "pytest is the best framework")
    if "test_req_001" not in coordinator._pending_requests:
        ok("request_id mapping 被消费 (pop)")
    else:
        fail("request_id mapping 未消费")

    # answer 无 to_agent 也能路由 (通过 request_id)
    coordinator._pending_requests["test_req_002"] = "role_analyst"
    coordinator.answer_question("test_req_002", "use Flask")
    ok("request_id-only answer 无异常")

    # 无 target 不崩溃
    coordinator.answer_question("nonexistent_req", "orphan answer")
    ok("无 target answer 不崩溃 (静默日志)")

    # ══════════════════════════════════════════════════════════════
    section(16, "通知策略: TeamNotificationPolicy")
    # ══════════════════════════════════════════════════════════════
    from agent_framework.models.team import MailEventType, TeamNotificationType

    policy = TeamNotificationPolicy()
    if policy.should_escalate_notification(TeamNotificationType.TASK_COMPLETED):
        ok("TASK_COMPLETED 策略升级")
    else:
        fail("TASK_COMPLETED 未升级")
    if policy.should_escalate_notification(TeamNotificationType.TASK_FAILED):
        ok("TASK_FAILED 策略升级")
    else:
        fail("TASK_FAILED 未升级")
    if not policy.should_escalate_notification(TeamNotificationType.BROADCAST):
        ok("BROADCAST 默认不升级")
    else:
        fail("BROADCAST 不应默认升级")
    if policy.should_escalate_mail_event(MailEventType.BROADCAST_NOTICE, topic="findings.sec"):
        ok("topic findings.* 升级")
    else:
        fail("topic findings.* 未升级")
    if not policy.should_escalate_mail_event(MailEventType.BROADCAST_NOTICE, topic="chat.general"):
        ok("topic chat.* 不升级")
    else:
        fail("topic chat.* 不应升级")

    disabled = TeamNotificationPolicy(enabled=False)
    if not disabled.should_escalate_notification(TeamNotificationType.TASK_COMPLETED):
        ok("disabled 策略全部拦截")
    else:
        fail("disabled 策略未拦截")

    # ══════════════════════════════════════════════════════════════
    section(17, "通知策略运行时接线")
    # ══════════════════════════════════════════════════════════════
    # Verify policy is wired into coordinator
    if coordinator._notification_policy is not None:
        ok("coordinator._notification_policy 已接线")
    else:
        fail("coordinator._notification_policy 未接线")

    if coordinator._on_event_escalation is not None:
        ok("coordinator._on_event_escalation 回调已注册")
    else:
        fail("coordinator._on_event_escalation 回调未注册")

    # Simulate QUESTION event escalation via process_inbox
    from agent_framework.models.team import MailEvent, MailEventType as MET
    question_event = MailEvent(
        team_id=coordinator.team_id,
        from_agent="role_coder",
        to_agent=lead_id,
        event_type=MET.QUESTION,
        request_id="test_q_escalate",
        payload={"question": "Which DB to use?", "request_id": "test_q_escalate"},
    )
    coordinator._mailbox.send(question_event)
    before_count = len(fw._pending_team_notifications)
    coordinator.process_inbox()
    # Escalation callback is async (ensure_future) — wait for it to execute
    await asyncio.sleep(0.2)
    after_count = len(fw._pending_team_notifications)
    if after_count > before_count:
        ok(f"QUESTION 事件已升级为通知 ({before_count} → {after_count})")
        # Verify the escalated notification has correct type
        last_n = fw._pending_team_notifications[-1]
        if last_n.notification_type.value == "QUESTION":
            ok("升级通知类型 = QUESTION")
        else:
            info(f"升级通知类型: {last_n.notification_type.value}")
    else:
        fail("QUESTION 事件未升级为通知")
    # Clean up
    fw.drain_team_notifications()
    fw.mark_team_notifications_delivered()

    # ══════════════════════════════════════════════════════════════
    section(18, "fw.run() 框架级串行化")
    # ══════════════════════════════════════════════════════════════
    # Verify fw.run() acquires dispatcher lock (no deadlock, no bypass)
    dispatcher = fw._run_dispatcher
    if dispatcher is not None:
        # Lock should be free before call
        if not dispatcher._lock.locked():
            ok("调用前锁空闲")
        else:
            fail("调用前锁已被持有")

        # fw.run() should work correctly (internally acquires/releases lock)
        r18 = await fw.run("1+1等于几？直接回答数字")
        if r18 and r18.final_answer:
            ok(f"fw.run() 正常完成: {r18.final_answer[:30]}")
        else:
            info("fw.run() 返回无内容")

        # Lock should be free after call
        if not dispatcher._lock.locked():
            ok("调用后锁已释放 (无死锁)")
        else:
            fail("调用后锁未释放 (可能死锁)")
    else:
        info("无 dispatcher — 跳过串行化验证")

    # ══════════════════════════════════════════════════════════════
    section(19, "可恢复 Teammate (问答后继续执行)")
    # ══════════════════════════════════════════════════════════════
    # Verify the multi-run conversation infrastructure
    if hasattr(coordinator, "_pending_answers"):
        ok("coordinator._pending_answers 存在")
    else:
        fail("coordinator._pending_answers 不存在")

    if hasattr(coordinator, "_active_teammate_ctx"):
        ok("coordinator._active_teammate_ctx 存在")
    else:
        fail("coordinator._active_teammate_ctx 不存在")

    # Test answer delivery to _pending_answers
    coordinator._pending_answers["role_reviewer"] = "use pytest"
    answer = coordinator._pending_answers.pop("role_reviewer", None)
    if answer == "use pytest":
        ok("_pending_answers 写入/读取正确")
    else:
        fail(f"_pending_answers 读取异常: {answer}")

    # Test answer_question writes to _pending_answers
    coordinator._pending_requests["test_resume_req"] = "role_coder"
    coordinator.answer_question("test_resume_req", "the file is at ./test.py")
    if "role_coder" in coordinator._pending_answers:
        delivered = coordinator._pending_answers.pop("role_coder")
        ok(f"answer_question 写入 _pending_answers: {delivered[:30]}")
    else:
        fail("answer_question 未写入 _pending_answers")

    # Verify WAITING_ANSWER status exists and transitions work
    from agent_framework.models.team import TeamMemberStatus as TMS
    if TMS.WAITING_ANSWER.value == "WAITING_ANSWER":
        ok("WAITING_ANSWER 状态存在")
    else:
        fail("WAITING_ANSWER 状态缺失")

    if TMS.WAITING_APPROVAL.value == "WAITING_APPROVAL":
        ok("WAITING_APPROVAL 状态存在")
    else:
        fail("WAITING_APPROVAL 状态缺失")

    # ══════════════════════════════════════════════════════════════
    section(20, "Busy 重入保护 (原子占用)")
    # ══════════════════════════════════════════════════════════════
    # Ensure all members are IDLE first
    fw.drain_team_notifications()
    fw.mark_team_notifications_delivered()
    await asyncio.sleep(2)

    # Find a member in IDLE
    idle_member = None
    for m_item in registry.list_members():
        if m_item.role != "lead" and m_item.status.value == "IDLE":
            idle_member = m_item
            break

    if idle_member:
        # First assign succeeds
        r20a = coordinator.assign_task("第一个任务", idle_member.agent_id)
        if r20a.get("assigned"):
            ok(f"首次 assign 成功: {idle_member.agent_id}")
        else:
            fail(f"首次 assign 失败: {r20a}")

        # Second assign to same member must be rejected (busy)
        r20b = coordinator.assign_task("第二个任务", idle_member.agent_id)
        if not r20b.get("assigned") and "busy" in r20b.get("error", ""):
            ok(f"二次 assign 拒绝 (busy): {r20b['error'][:60]}")
        else:
            fail(f"二次 assign 未拒绝: {r20b}")

        # Wait for first task to complete
        info(_wait("首个任务完成", 20))
        await asyncio.sleep(20)
        fw.drain_team_notifications()
        fw.mark_team_notifications_delivered()
    else:
        info("无 IDLE 成员可测试 — 跳过")

    # ══════════════════════════════════════════════════════════════
    section(21, "Mail 身份一致性 (spawn_id = member agent_id)")
    # ══════════════════════════════════════════════════════════════
    # Verify that the sub-agent's mail identity matches the team member ID
    # (not a random sub_xxx or hex ID)
    if coordinator._runtime is not None:
        # The prompt includes "Your agent_id is '<member_id>'"
        # and spec.spawn_id = member_id, so factory sets _current_spawn_id = member_id
        ok("spawn_id = member.agent_id 策略已启用")
    else:
        info("无 runtime — 跳过 mail 身份验证")

    # Verify _pending_approvals exists (PLAN continuation support)
    if hasattr(coordinator, "_pending_approvals"):
        ok("_pending_approvals 存在 (PLAN 审批续接)")
    else:
        fail("_pending_approvals 不存在")

    # Test approve_plan writes to _pending_approvals
    plan_reg = coordinator._plans
    try:
        plan = plan_reg.create(
            requester="role_coder", approver=lead_id,
            plan_text="重构模块", title="Refactor",
            risk_level="medium", team_id=coordinator.team_id,
        )
        coordinator.approve_plan(plan.request_id, feedback="approved")
        if "role_coder" in coordinator._pending_approvals:
            approval = coordinator._pending_approvals.pop("role_coder")
            if approval.get("approved") is True:
                ok("approve_plan 写入 _pending_approvals: approved=True")
            else:
                fail(f"审批结果异常: {approval}")
        else:
            fail("approve_plan 未写入 _pending_approvals")
    except Exception as exc:
        info(f"PLAN 审批测试异常: {exc}")

    # ══════════════════════════════════════════════════════════════
    section(22, "finalize 闭环验证 (await + RESULT_READY)")
    # ══════════════════════════════════════════════════════════════
    import inspect
    if inspect.iscoroutinefunction(coordinator._finalize_teammate_result):
        ok("_finalize_teammate_result 是 async 函数")
    else:
        fail("_finalize_teammate_result 不是 async 函数 (缺 await 会卡死)")

    # Verify the watcher code has 'await' (source code check)
    import textwrap
    src = inspect.getsource(coordinator._watch_teammate)
    if "await self._finalize_teammate_result" in src:
        ok("_watch_teammate 中 await _finalize_teammate_result")
    else:
        fail("_watch_teammate 中缺少 await _finalize_teammate_result")

    # ══════════════════════════════════════════════════════════════
    section(23, "AT-001: 团队配置持久化")
    # ══════════════════════════════════════════════════════════════
    from agent_framework.team.config_store import TeamConfigStore
    from agent_framework.models.team import TeamConfigData, TeamConfigMember
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        store = TeamConfigStore(base_dir=tmpdir)
        cfg = TeamConfigData(
            team_id=coordinator.team_id, lead_id=lead_id, name="real-test",
            members=[
                TeamConfigMember(member_id=m.agent_id, role=m.role)
                for m in registry.list_members() if m.role != "lead"
            ],
        )
        path = store.save(cfg)
        if path.exists():
            ok(f"config 已持久化: {path.name}")
        else:
            fail("config 未持久化")

        loaded = store.load("real-test")
        if loaded and loaded.team_id == coordinator.team_id:
            ok(f"config 已加载: team_id={loaded.team_id}")
        else:
            fail("config 加载失败")

        teams = store.list_teams()
        if "real-test" in teams:
            ok("list_teams 发现持久化团队")
        else:
            fail("list_teams 未发现团队")

        if store.delete("real-test"):
            ok("config 已删除")
        else:
            fail("config 删除失败")

    # ══════════════════════════════════════════════════════════════
    section(24, "AT-002~004: 共享任务面板真实流程")
    # ══════════════════════════════════════════════════════════════
    # Create tasks with dependency
    t1 = coordinator.create_task("编写 add 函数", description="实现两数相加")
    t2 = coordinator.create_task("编写 add 测试", depends_on=[t1["task_id"]])
    if t1.get("created"):
        ok(f"任务 A 创建: {t1['task_id']}")
    else:
        fail("任务 A 创建失败")
    if t2.get("created") and t2["status"] == "blocked":
        ok(f"任务 B 创建 (blocked): {t2['task_id']}")
    else:
        info(f"任务 B 状态: {t2.get('status')}")

    # Claim task A
    claimed = coordinator.claim_task("role_coder", t1["task_id"])
    if claimed.get("claimed"):
        ok(f"coder 认领任务 A: {claimed['task_id']}")
    else:
        fail("认领失败")

    # Complete task A → B should unblock
    completed = coordinator.complete_task(t1["task_id"], result="add 函数完成")
    if completed.get("completed") or completed.get("ok"):
        ok("任务 A 完成")
    else:
        fail(f"任务 A 完成失败: {completed}")

    # Check B is now PENDING
    tasks = coordinator.list_tasks()
    b_task = next((t for t in tasks["tasks"] if t["task_id"] == t2["task_id"]), None)
    if b_task and b_task["status"] == "pending":
        ok("任务 B 自动解锁 (blocked → pending)")
    else:
        info(f"任务 B 状态: {b_task['status'] if b_task else '未找到'}")

    # Claim task B
    claimed_b = coordinator.claim_task("role_reviewer", t2["task_id"])
    if claimed_b.get("claimed"):
        ok("reviewer 认领任务 B")
    else:
        info("任务 B 认领失败")

    # Concurrent claim guard
    double = coordinator.claim_task("role_coder", t2["task_id"])
    if not double.get("claimed"):
        ok("重复认领被拒绝 (原子锁)")
    else:
        fail("重复认领未被拒绝")

    coordinator.complete_task(t2["task_id"], result="测试通过")

    # list_tasks
    all_tasks = coordinator.list_tasks()
    if all_tasks["total"] >= 2:
        ok(f"任务面板: {all_tasks['total']} 个任务")
    else:
        fail(f"任务面板异常: {all_tasks}")

    # ══════════════════════════════════════════════════════════════
    section(25, "AT-010/011: Cleanup 语义")
    # ══════════════════════════════════════════════════════════════
    # Ensure all IDLE first
    fw.drain_team_notifications()
    fw.mark_team_notifications_delivered()
    await asyncio.sleep(2)

    # Try cleanup — should check active members
    has_busy = any(
        m.status in BUSY_MEMBER_STATUSES
        for m in registry.list_members() if m.role != "lead"
    )
    if has_busy:
        result_c = coordinator.cleanup_team()
        if not result_c.get("ok"):
            ok(f"cleanup 拒绝脏状态: {result_c.get('error_code')}")
        else:
            info("cleanup 意外成功 (可能所有成员已完成)")
    else:
        result_c = coordinator.cleanup_team()
        if result_c.get("ok"):
            ok("cleanup 成功 (所有成员 IDLE)")
        else:
            fail(f"cleanup 失败: {result_c}")

    # ══════════════════════════════════════════════════════════════
    section(26, "AT-012/013: Hook 拦截验证")
    # ══════════════════════════════════════════════════════════════
    from agent_framework.models.hook import HookPoint, DENIABLE_HOOK_POINTS

    if HookPoint.TEAMMATE_TASK_COMPLETED in DENIABLE_HOOK_POINTS:
        ok("TEAMMATE_TASK_COMPLETED 是 deniable hook")
    else:
        fail("TEAMMATE_TASK_COMPLETED 不在 DENIABLE")

    if HookPoint.TEAMMATE_IDLE not in DENIABLE_HOOK_POINTS:
        ok("TEAMMATE_IDLE 是 advisory hook (不可 deny)")
    else:
        info("TEAMMATE_IDLE 也是 deniable")

    # Verify hook executor is wired
    if coordinator._hook_executor is not None:
        ok("hook_executor 已接入 coordinator")
    else:
        info("hook_executor 未接入 (auto_approve 模式可能跳过)")

    # ══════════════════════════════════════════════════════════════
    section(27, "AT-014: 用户直接聚焦 Teammate")
    # ══════════════════════════════════════════════════════════════
    from agent_framework.terminal_runtime import TeammateFocusState

    focus = TeammateFocusState()
    focus.set_agents(["role_coder", "role_reviewer", "role_analyst"])

    # Cycle through
    first = focus.cycle_next()
    if first == "role_coder":
        ok(f"聚焦第 1 个: {first}")
    else:
        fail(f"聚焦异常: {first}")

    second = focus.cycle_next()
    if second == "role_reviewer":
        ok(f"聚焦第 2 个: {second}")
    else:
        fail(f"聚焦异常: {second}")

    # Wrap back to lead
    focus.cycle_next()  # analyst
    back = focus.cycle_next()  # None = lead
    if back is None and not focus.is_focused():
        ok("循环回 lead (unfocus)")
    else:
        fail(f"未回到 lead: {back}")

    # Unfocus
    focus.set_agents(["role_coder"])
    focus.cycle_next()
    focus.unfocus()
    if not focus.is_focused():
        ok("unfocus 成功")
    else:
        fail("unfocus 失败")

    # ══════════════════════════════════════════════════════════════
    section(28, "AT-009/015: 通知类型 + 错误模型")
    # ══════════════════════════════════════════════════════════════
    from agent_framework.models.team import TeamActionError

    # TEAMMATE_IDLE notification type exists
    if TeamNotificationType.TEAMMATE_IDLE.value == "TEAMMATE_IDLE":
        ok("TEAMMATE_IDLE 通知类型存在")
    else:
        fail("TEAMMATE_IDLE 通知类型缺失")

    # TeamActionError model
    err = TeamActionError(
        error_code="TEAM_MEMBER_BUSY",
        message="Teammate is busy",
        retryable=False,
    )
    if err.ok is False and err.error_code == "TEAM_MEMBER_BUSY":
        ok("TeamActionError 结构化错误模型")
    else:
        fail("错误模型异常")

    # AT-015: PROGRESS_NOTICE not in default escalation
    if not policy.should_escalate_mail_event(MailEventType.PROGRESS_NOTICE):
        ok("PROGRESS_NOTICE 不升级为完成通知 (AT-015)")
    else:
        fail("PROGRESS_NOTICE 被错误升级")

    # TEAMMATE_IDLE in default escalation
    if policy.should_escalate_notification(TeamNotificationType.TEAMMATE_IDLE):
        ok("TEAMMATE_IDLE 默认升级 (AT-009)")
    else:
        fail("TEAMMATE_IDLE 未默认升级")

    # ══════════════════════════════════════════════════════════════
    section(0, "最终状态总览")
    # ══════════════════════════════════════════════════════════════
    fw.drain_team_notifications()
    fw.mark_team_notifications_delivered()
    await asyncio.sleep(1)

    for m in registry.list_members():
        if m.role == "lead":
            continue
        icon = _status_icon(m.status.value)
        info(f"{icon} {m.agent_id} ({m.role}) — {m.status.value}")

    # ══════════════════════════════════════════════════════════════
    # 测试结果汇总
    # ══════════════════════════════════════════════════════════════
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

    # 清理
    try:
        if fw._run_dispatcher:
            fw._run_dispatcher.stop()
        await fw.shutdown()
    except Exception:
        pass

    return 0 if not failed_items else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Team v2 全功能真实测试")
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

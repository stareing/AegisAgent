#!/usr/bin/env python3
"""AT-* 合规验证脚本 — 独立验证新增功能的数据流一致性。

仅测试 Phase 3~5 新增能力，不依赖 LLM 真实执行：
  AT-001: 团队配置持久化 (save/load/delete/list)
  AT-002~004: 共享任务面板 (create→claim→complete→unblock + 并发)
  AT-009: TEAMMATE_IDLE 通知升级
  AT-010: Cleanup 拒绝脏状态
  AT-011: Cleanup 清理资源
  AT-012: Hook 拦截 complete_task
  AT-013: Hook 拦截 mark_result_delivered
  AT-014: 用户聚焦 Teammate (TeammateFocusState)
  AT-015: PROGRESS_NOTICE 不升级
  错误模型: TeamActionError 结构化返回

使用:
    python scripts/test_team_at_compliance.py
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock

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


def section(t: str) -> None:
    print(f"\n{'━'*60}\n  {bold(magenta(t))}\n{'━'*60}")


def _make_team():
    from agent_framework.models.team import TeamMember, TeamMemberStatus
    from agent_framework.notification.bus import AgentBus
    from agent_framework.notification.persistence import InMemoryBusPersistence
    from agent_framework.team.coordinator import TeamCoordinator
    from agent_framework.team.mailbox import TeamMailbox
    from agent_framework.team.plan_registry import PlanRegistry
    from agent_framework.team.registry import TeamRegistry
    from agent_framework.team.shutdown_registry import ShutdownRegistry

    bus = AgentBus(persistence=InMemoryBusPersistence())
    registry = TeamRegistry("team_test")
    mailbox = TeamMailbox(bus, registry)
    for aid, role, status in [
        ("lead_001", "lead", TeamMemberStatus.WORKING),
        ("role_coder", "coder", TeamMemberStatus.IDLE),
        ("role_reviewer", "reviewer", TeamMemberStatus.IDLE),
        ("role_analyst", "analyst", TeamMemberStatus.IDLE),
    ]:
        registry.register(TeamMember(
            agent_id=aid, team_id="team_test", role=role, status=status,
        ))
    coord = TeamCoordinator(
        "team_test", "lead_001", mailbox, registry,
        PlanRegistry(), ShutdownRegistry(),
    )
    return coord, mailbox, registry


def main() -> int:
    from agent_framework.models.team import (
        BUSY_MEMBER_STATUSES,
        MailEventType,
        TeamActionError,
        TeamConfigData,
        TeamConfigMember,
        TeamMemberStatus,
        TeamNotificationType,
        TeamSessionState,
    )
    from agent_framework.team.task_board import TaskStatus, TeamTaskBoard

    print(f"\n{bold('AT-* 合规验证 — 新功能数据流一致性')}\n")

    # ═══════════════════════════════════════════════════════════
    section("AT-001: 团队配置持久化")
    # ═══════════════════════════════════════════════════════════
    from agent_framework.team.config_store import TeamConfigStore

    with tempfile.TemporaryDirectory() as tmpdir:
        store = TeamConfigStore(base_dir=tmpdir)

        # Save
        cfg = TeamConfigData(
            team_id="team_abc", lead_id="lead_001", name="my-team",
            members=[
                TeamConfigMember(member_id="role_coder", role="coder"),
                TeamConfigMember(member_id="role_reviewer", role="reviewer"),
            ],
        )
        path = store.save(cfg)
        if path.exists():
            ok(f"save → {path.name} 写入成功")
        else:
            fail("save 失败")

        # Load
        loaded = store.load("my-team")
        if loaded and loaded.team_id == "team_abc" and len(loaded.members) == 2:
            ok(f"load → team_id={loaded.team_id}, members={len(loaded.members)}")
        else:
            fail(f"load 失败: {loaded}")

        # Load 字段完整性
        if loaded.lead_id == "lead_001" and loaded.name == "my-team":
            ok("load 字段完整 (lead_id, name)")
        else:
            fail("load 字段缺失")

        if loaded.created_at and loaded.updated_at:
            ok("load 时间戳存在")
        else:
            fail("load 时间戳缺失")

        # List
        store.save(TeamConfigData(team_id="t2", lead_id="l", name="second"))
        teams = store.list_teams()
        if "my-team" in teams and "second" in teams:
            ok(f"list_teams → {teams}")
        else:
            fail(f"list_teams 异常: {teams}")

        # Delete
        deleted = store.delete("my-team")
        if deleted and store.load("my-team") is None:
            ok("delete → 删除成功 + load 返回 None")
        else:
            fail("delete 失败")

        # Load nonexistent
        if store.load("nonexistent") is None:
            ok("load 不存在团队 → None")
        else:
            fail("load 不存在团队应返回 None")

    # ═══════════════════════════════════════════════════════════
    section("AT-002~004: 共享任务面板")
    # ═══════════════════════════════════════════════════════════
    coord, mailbox, registry = _make_team()

    # Create tasks with dependency chain: A → B → C
    t_a = coord.create_task("Task A: 编写核心模块")
    t_b = coord.create_task("Task B: 编写测试", depends_on=[t_a["task_id"]])
    t_c = coord.create_task("Task C: 代码审查", depends_on=[t_b["task_id"]])

    if t_a["created"] and t_a["status"] == "pending":
        ok(f"A 创建: {t_a['task_id']} (pending)")
    else:
        fail(f"A 创建异常: {t_a}")
    if t_b["created"] and t_b["status"] == "blocked":
        ok(f"B 创建: {t_b['task_id']} (blocked, depends_on A)")
    else:
        fail(f"B 创建异常: {t_b}")
    if t_c["created"] and t_c["status"] == "blocked":
        ok(f"C 创建: {t_c['task_id']} (blocked, depends_on B)")
    else:
        fail(f"C 创建异常: {t_c}")

    # Claim A
    claim_a = coord.claim_task("role_coder", t_a["task_id"])
    if claim_a["claimed"]:
        ok(f"coder 认领 A: {claim_a['task_id']}")
    else:
        fail("A 认领失败")

    # Cannot claim B (blocked)
    claim_b_blocked = coord.claim_task("role_reviewer", t_b["task_id"])
    if not claim_b_blocked["claimed"]:
        ok("B 不可认领 (blocked)")
    else:
        fail("B 应被阻塞")

    # Auto-claim skips blocked
    auto = coord.claim_task("role_analyst")
    if not auto["claimed"]:
        ok("auto-claim 无可用任务 (A 已认领, B/C blocked)")
    else:
        fail("auto-claim 不应成功")

    # Complete A → B unblocks
    comp_a = coord.complete_task(t_a["task_id"], result="模块完成")
    if comp_a.get("ok") or comp_a.get("completed"):
        ok("A 完成")
    else:
        fail(f"A 完成失败: {comp_a}")

    tasks = coord.list_tasks()
    b_status = next(
        (t["status"] for t in tasks["tasks"] if t["task_id"] == t_b["task_id"]), None
    )
    c_status = next(
        (t["status"] for t in tasks["tasks"] if t["task_id"] == t_c["task_id"]), None
    )
    if b_status == "pending":
        ok("B 自动解锁 → pending")
    else:
        fail(f"B 状态异常: {b_status}")
    if c_status == "blocked":
        ok("C 仍 blocked (B 未完成)")
    else:
        fail(f"C 状态异常: {c_status}")

    # Claim + complete B → C unblocks
    coord.claim_task("role_reviewer", t_b["task_id"])
    coord.complete_task(t_b["task_id"], result="测试通过")
    tasks2 = coord.list_tasks()
    c_status2 = next(
        (t["status"] for t in tasks2["tasks"] if t["task_id"] == t_c["task_id"]), None
    )
    if c_status2 == "pending":
        ok("C 自动解锁 → pending (链式解锁)")
    else:
        fail(f"C 链式解锁失败: {c_status2}")

    # list_tasks summary
    if tasks2["total"] == 3 and tasks2["claimable"] == 1:
        ok(f"list_tasks: total=3, claimable=1")
    else:
        info(f"list_tasks: {tasks2}")

    # Concurrent claim test — use a fresh board with exactly 1 task
    fresh_board = TeamTaskBoard("concurrent_test")
    fresh_board.create_task("Only one")
    winners = []

    def try_claim(agent_id):
        r = fresh_board.claim_task(agent_id)
        if r:
            winners.append(agent_id)

    threads = [threading.Thread(target=try_claim, args=(f"t{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if len(winners) == 1:
        ok(f"10 线程并发认领: 仅 {winners[0]} 成功")
    else:
        fail(f"并发认领异常: {len(winners)} 个赢家")

    # Fail task
    task_board = coord._task_board
    task_board.create_task("Will fail")
    fail_t = task_board.list_claimable()[-1]
    task_board.claim_task("agent_x", fail_t.task_id)
    task_board.fail_task(fail_t.task_id, "timeout")
    if task_board.get_task(fail_t.task_id).status == TaskStatus.FAILED:
        ok("fail_task → FAILED")
    else:
        fail("fail_task 异常")

    # ═══════════════════════════════════════════════════════════
    section("AT-010/011: Cleanup 语义")
    # ═══════════════════════════════════════════════════════════
    coord2, _, reg2 = _make_team()

    # All IDLE → cleanup succeeds
    result_clean = coord2.cleanup_team()
    if result_clean["ok"]:
        ok("全 IDLE cleanup 成功")
    else:
        fail(f"cleanup 失败: {result_clean}")

    # After cleanup, task board is None
    if coord2._task_board is None:
        ok("cleanup 清除 task_board")
    else:
        fail("task_board 未清除")

    # Re-create team with busy member
    coord3, _, reg3 = _make_team()
    reg3.update_status("role_coder", TeamMemberStatus.WORKING)
    result_dirty = coord3.cleanup_team()
    if not result_dirty["ok"] and result_dirty["error_code"] == "TEAM_CLEANUP_ACTIVE_MEMBERS":
        ok(f"WORKING 成员 cleanup 拒绝: {result_dirty['error_code']}")
    else:
        fail(f"cleanup 应拒绝: {result_dirty}")

    if "role_coder" in result_dirty.get("active_members", []):
        ok("active_members 包含 role_coder")
    else:
        fail("active_members 未包含 role_coder")

    # WAITING_ANSWER also blocked
    coord4, _, reg4 = _make_team()
    reg4.update_status("role_reviewer", TeamMemberStatus.WAITING_ANSWER)
    result_wait = coord4.cleanup_team()
    if not result_wait["ok"]:
        ok("WAITING_ANSWER cleanup 拒绝")
    else:
        fail("WAITING_ANSWER 应拒绝 cleanup")

    # SHUTDOWN members allowed
    coord5, _, reg5 = _make_team()
    reg5.update_status("role_coder", TeamMemberStatus.SHUTDOWN)
    reg5.update_status("role_reviewer", TeamMemberStatus.SHUTDOWN)
    reg5.update_status("role_analyst", TeamMemberStatus.SHUTDOWN)
    result_shut = coord5.cleanup_team()
    if result_shut["ok"]:
        ok("全 SHUTDOWN cleanup 成功")
    else:
        fail(f"SHUTDOWN cleanup 应成功: {result_shut}")

    # ═══════════════════════════════════════════════════════════
    section("AT-012: Hook 拦截 complete_task")
    # ═══════════════════════════════════════════════════════════
    from agent_framework.models.hook import HookPoint, DENIABLE_HOOK_POINTS

    if HookPoint.TEAMMATE_TASK_COMPLETED in DENIABLE_HOOK_POINTS:
        ok("TEAMMATE_TASK_COMPLETED 可拦截 (deniable)")
    else:
        fail("TEAMMATE_TASK_COMPLETED 不在 DENIABLE")

    coord6, _, _ = _make_team()
    coord6.create_task("Hook test")
    task_id = coord6.list_tasks()["tasks"][0]["task_id"]
    coord6.claim_task("role_coder", task_id)

    # Mock hook that denies
    mock_hook = MagicMock()
    deny = MagicMock()
    deny.action = "DENY"
    deny.feedback = "Need unit tests"
    mock_hook.fire_sync_advisory = MagicMock(return_value=deny)
    coord6._hook_executor = mock_hook

    result_denied = coord6.complete_task(task_id, result="Done without tests")
    if result_denied.get("ok") is False and result_denied["error_code"] == "TEAM_HOOK_DENIED":
        ok(f"hook DENY → 完成被拒: {result_denied['message']}")
    else:
        fail(f"hook DENY 未生效: {result_denied}")

    # Verify task still IN_PROGRESS
    task_after = coord6._task_board.get_task(task_id)
    if task_after.status == TaskStatus.IN_PROGRESS:
        ok("hook DENY 后任务仍 IN_PROGRESS (状态未推进)")
    else:
        fail(f"状态异常: {task_after.status}")

    # Reset hook → allow
    coord6._hook_executor = None
    result_ok = coord6.complete_task(task_id, result="Done with tests")
    if result_ok.get("ok") or result_ok.get("completed"):
        ok("无 hook 时正常完成")
    else:
        fail(f"完成失败: {result_ok}")

    # ═══════════════════════════════════════════════════════════
    section("AT-013: TEAMMATE_IDLE hook (advisory, not deniable)")
    # ═══════════════════════════════════════════════════════════
    coord7, _, reg7 = _make_team()
    reg7.update_status("role_coder", TeamMemberStatus.WORKING)
    reg7.update_status("role_coder", TeamMemberStatus.RESULT_READY)
    reg7.update_status("role_coder", TeamMemberStatus.NOTIFYING)

    # Hook fires but CANNOT block — TEAMMATE_IDLE is advisory
    mock_hook2 = MagicMock()
    mock_hook2.fire_sync_advisory = MagicMock(return_value=None)
    coord7._hook_executor = mock_hook2

    coord7.mark_result_delivered("role_coder")
    if reg7.get("role_coder").status == TeamMemberStatus.IDLE:
        ok("TEAMMATE_IDLE hook advisory → 转 IDLE (不可阻塞)")
    else:
        fail(f"状态异常: {reg7.get('role_coder').status}")

    # Verify hook was called
    if mock_hook2.fire_sync_advisory.called:
        ok("hook 被触发 (advisory)")
    else:
        fail("hook 未触发")

    # ═══════════════════════════════════════════════════════════
    section("AT-014: 用户聚焦 Teammate")
    # ═══════════════════════════════════════════════════════════
    from agent_framework.terminal_runtime import TeammateFocusState, ReplState

    focus = TeammateFocusState()
    focus.set_agents(["role_coder", "role_reviewer", "role_analyst"])

    # 初始未聚焦
    if not focus.is_focused():
        ok("初始: 未聚焦 (lead 模式)")
    else:
        fail("初始应未聚焦")

    # 循环 3 个 + 回到 lead
    f1 = focus.cycle_next()
    if f1 == "role_coder" and focus.is_focused():
        ok(f"cycle 1: {f1} (聚焦)")
    else:
        fail(f"cycle 1 异常: {f1}")

    f2 = focus.cycle_next()
    if f2 == "role_reviewer":
        ok(f"cycle 2: {f2}")
    else:
        fail(f"cycle 2 异常: {f2}")

    f3 = focus.cycle_next()
    if f3 == "role_analyst":
        ok(f"cycle 3: {f3}")
    else:
        fail(f"cycle 3 异常: {f3}")

    f4 = focus.cycle_next()
    if f4 is None and not focus.is_focused():
        ok("cycle 4: 回到 lead (None)")
    else:
        fail(f"cycle 4 异常: {f4}")

    # unfocus
    focus.cycle_next()
    focus.unfocus()
    if not focus.is_focused():
        ok("unfocus 成功")
    else:
        fail("unfocus 失败")

    # ReplState integration
    state = ReplState()
    if hasattr(state, "teammate_focus") and isinstance(state.teammate_focus, TeammateFocusState):
        ok("ReplState.teammate_focus 存在")
    else:
        fail("ReplState.teammate_focus 缺失")

    # ═══════════════════════════════════════════════════════════
    section("AT-009: TEAMMATE_IDLE 通知升级")
    # ═══════════════════════════════════════════════════════════
    from agent_framework.team.notification_policy import TeamNotificationPolicy

    policy = TeamNotificationPolicy()

    if policy.should_escalate_notification(TeamNotificationType.TEAMMATE_IDLE):
        ok("TEAMMATE_IDLE 在默认升级类型中")
    else:
        fail("TEAMMATE_IDLE 未在默认升级类型中")

    if TeamNotificationType.TEAMMATE_IDLE.value == "TEAMMATE_IDLE":
        ok("TeamNotificationType.TEAMMATE_IDLE 枚举值正确")
    else:
        fail("枚举值异常")

    # ═══════════════════════════════════════════════════════════
    section("AT-015: PROGRESS 不作为完成")
    # ═══════════════════════════════════════════════════════════
    if not policy.should_escalate_mail_event(MailEventType.PROGRESS_NOTICE):
        ok("PROGRESS_NOTICE 不在默认升级 (不触发完成总结)")
    else:
        fail("PROGRESS_NOTICE 不应被升级")

    if policy.should_escalate_mail_event(MailEventType.ERROR_NOTICE):
        ok("ERROR_NOTICE 在默认升级")
    else:
        fail("ERROR_NOTICE 应被升级")

    if policy.should_escalate_mail_event(MailEventType.QUESTION):
        ok("QUESTION 在默认升级")
    else:
        fail("QUESTION 应被升级")

    # ═══════════════════════════════════════════════════════════
    section("错误模型 (spec §11)")
    # ═══════════════════════════════════════════════════════════
    err = TeamActionError(
        error_code="TEAM_MEMBER_BUSY",
        message="Teammate 'role_coder' is busy",
        retryable=False,
    )
    if err.ok is False:
        ok("TeamActionError.ok = False")
    if err.error_code == "TEAM_MEMBER_BUSY":
        ok("error_code = TEAM_MEMBER_BUSY")
    if err.retryable is False:
        ok("retryable = False")

    # cleanup 返回结构化错误
    coord_err, _, reg_err = _make_team()
    reg_err.update_status("role_coder", TeamMemberStatus.WORKING)
    result_err = coord_err.cleanup_team()
    required_fields = ["ok", "error_code", "message"]
    missing = [f for f in required_fields if f not in result_err]
    if not missing:
        ok(f"cleanup 错误包含 {required_fields}")
    else:
        fail(f"cleanup 错误缺失: {missing}")

    # complete_task 返回结构化错误
    coord_err2, _, _ = _make_team()
    result_err2 = coord_err2.complete_task("nonexistent_task")
    if result_err2.get("ok") is False and "error_code" in result_err2:
        ok(f"complete_task 错误: {result_err2['error_code']}")
    else:
        fail(f"complete_task 应返回结构化错误: {result_err2}")

    # ═══════════════════════════════════════════════════════════
    section("数据模型完整性")
    # ═══════════════════════════════════════════════════════════
    # TeamSessionState model
    session = TeamSessionState(
        session_id="sess_001", team_id="team_test",
        member_id="role_coder", current_task_id="task_001",
    )
    if session.session_id == "sess_001" and session.member_id == "role_coder":
        ok("TeamSessionState 模型字段正确")
    else:
        fail("TeamSessionState 字段异常")

    # TeamConfigMember frozen
    member = TeamConfigMember(member_id="m1", role="coder")
    try:
        member.role = "hacker"  # type: ignore[misc]
        fail("TeamConfigMember 应为 frozen")
    except Exception:
        ok("TeamConfigMember 是 frozen (不可变)")

    # TeamActionError frozen
    try:
        err.message = "changed"  # type: ignore[misc]
        fail("TeamActionError 应为 frozen")
    except Exception:
        ok("TeamActionError 是 frozen (不可变)")

    # BUSY_MEMBER_STATUSES completeness
    expected_busy = {
        "SPAWNING", "WORKING", "WAITING_APPROVAL", "WAITING_ANSWER",
        "RESULT_READY", "NOTIFYING", "SHUTDOWN_REQUESTED",
    }
    actual_busy = {s.value for s in BUSY_MEMBER_STATUSES}
    if expected_busy == actual_busy:
        ok(f"BUSY_MEMBER_STATUSES: {len(actual_busy)} 项")
    else:
        fail(f"BUSY 不一致: 缺 {expected_busy - actual_busy}, 多 {actual_busy - expected_busy}")

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
    return 0 if not failed_items else 1


if __name__ == "__main__":
    sys.exit(main())

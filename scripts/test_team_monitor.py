#!/usr/bin/env python3
"""Team 全量监控脚本 — 实时追踪所有中间状态变化和事件流。

功能:
  - 注册 AgentBus 订阅，实时打印所有总线事件
  - 周期性轮询 Registry/Mailbox/Bus 状态
  - 追踪每个 teammate 的状态迁移链
  - 记录完整事件时序到日志文件
  - 最终输出全局时序图和状态报告

使用:
    python scripts/test_team_monitor.py
    python scripts/test_team_monitor.py --config config/doubao.local.json
    python scripts/test_team_monitor.py --interval 1.0    # 轮询间隔
    python scripts/test_team_monitor.py --log team_run.log
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
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
def blue(t: str) -> str: return _c(34, t)

STATUS_COLORS = {
    "WORKING": 33, "IDLE": 32, "SPAWNING": 36, "FAILED": 31,
    "SHUTDOWN": 90, "SHUTDOWN_REQUESTED": 31,
    "WAITING_APPROVAL": 35, "WAITING_ANSWER": 35,
}

EVENT_ICONS = {
    "TASK_ASSIGNMENT": "📋", "PROGRESS_NOTICE": "📊", "QUESTION": "❓",
    "ANSWER": "💬", "PLAN_SUBMISSION": "📝", "APPROVAL_RESPONSE": "✅",
    "SHUTDOWN_REQUEST": "🛑", "SHUTDOWN_ACK": "🏁", "BROADCAST_NOTICE": "📢",
    "ERROR_NOTICE": "❌", "STATUS_PING": "🏓", "STATUS_REPLY": "🏓",
    "TASK_CLAIM_REQUEST": "🙋", "TASK_HANDOFF_REQUEST": "🔄",
}


class EventLog:
    """收集所有事件和状态变化，最终输出时序报告。"""

    def __init__(self, log_file: str | None = None):
        self._entries: list[dict] = []
        self._state_history: dict[str, list[tuple[float, str]]] = defaultdict(list)
        self._t0 = time.monotonic()
        self._log_file = open(log_file, "w") if log_file else None

    def event(self, source: str, event_type: str, detail: str, **extra):
        elapsed = time.monotonic() - self._t0
        entry = {
            "t": round(elapsed, 2),
            "ts": datetime.now().strftime("%H:%M:%S.%f")[:12],
            "source": source,
            "type": event_type,
            "detail": detail,
            **extra,
        }
        self._entries.append(entry)
        if self._log_file:
            self._log_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._log_file.flush()

    def state_change(self, agent_id: str, new_status: str):
        elapsed = time.monotonic() - self._t0
        self._state_history[agent_id].append((round(elapsed, 2), new_status))

    def close(self):
        if self._log_file:
            self._log_file.close()

    def print_timeline(self):
        """打印完整事件时序。"""
        print(f"\n{'═'*70}")
        print(f"  {bold(magenta('事件时序 (共 ' + str(len(self._entries)) + ' 条)'))}")
        print(f"{'═'*70}")
        for e in self._entries:
            icon = EVENT_ICONS.get(e["type"], "·")
            src = cyan(e["source"][:15].ljust(15))
            typ = e["type"][:25].ljust(25)
            detail = e["detail"][:60]
            t_val = e["t"]
            print(f"  {dim(e['ts'])} {dim(f'+{t_val:6.1f}s')} {icon} {src} {typ} {detail}")

    def print_state_diagram(self):
        """打印每个 agent 的状态迁移链。"""
        print(f"\n{'═'*70}")
        print(f"  {bold(magenta('状态迁移链'))}")
        print(f"{'═'*70}")
        for agent_id, transitions in self._state_history.items():
            chain = []
            for t, status in transitions:
                color = STATUS_COLORS.get(status, 0)
                chain.append(f"{_c(color, status)}{dim(f'(+{t:.0f}s)')}")
            print(f"  {cyan(agent_id[:20].ljust(20))} {'→'.join(chain)}")


# ── 监控器 ─────────────────────────────────────────────────

class TeamMonitor:
    """实时监控 Team 所有状态和事件。"""

    def __init__(self, env: dict, event_log: EventLog):
        self._coordinator = env["coordinator"]
        self._mailbox = env["mailbox"]
        self._registry = env["registry"]
        self._bus = env["bus"]
        self._log = event_log
        self._known_statuses: dict[str, str] = {}
        self._bus_event_count = 0

    def setup_bus_listener(self):
        """订阅总线所有事件，实时打印。"""
        def _on_bus_event(envelope):
            self._bus_event_count += 1
            topic = envelope.topic
            src = envelope.source.agent_id
            payload_preview = str(envelope.payload)[:80]

            # 提取 mail event type
            mail_type = envelope.payload.get("_mail_event_type", "")
            icon = EVENT_ICONS.get(mail_type, "📨")

            self._log.event(src, mail_type or topic, payload_preview)

            tgt = envelope.target.agent_id if envelope.target else "*"
            print(
                f"  {dim(datetime.now().strftime('%H:%M:%S'))} "
                f"{icon} {blue('BUS')} "
                f"{cyan(src[:12])}→{cyan(tgt[:12])} "
                f"{yellow(mail_type or topic[:20])} "
                f"{dim(payload_preview[:50])}"
            )

        self._bus.subscribe("**", _on_bus_event)

    def poll_status(self):
        """轮询一次 Registry 状态，检测变化。"""
        members = self._registry.list_members()
        changes = []
        for m in members:
            status_str = m.status.value
            prev = self._known_statuses.get(m.agent_id)
            if prev != status_str:
                self._known_statuses[m.agent_id] = status_str
                self._log.state_change(m.agent_id, status_str)
                if prev is not None:
                    changes.append((m.agent_id, m.role, prev, status_str))
                    color = STATUS_COLORS.get(status_str, 0)
                    print(
                        f"  {dim(datetime.now().strftime('%H:%M:%S'))} "
                        f"🔄 {green('STATE')} "
                        f"{cyan(m.agent_id[:16])} ({m.role}) "
                        f"{dim(prev)} → {_c(color, status_str)}"
                    )
                else:
                    self._log.state_change(m.agent_id, status_str)
        return changes

    def print_dashboard(self):
        """打印当前状态仪表板。"""
        members = self._registry.list_members()
        print(f"\n  {'─'*55}")
        print(f"  {bold('Dashboard')} | Team: {self._coordinator.team_id} | Bus events: {self._bus_event_count}")
        print(f"  {'─'*55}")
        for m in members:
            color = STATUS_COLORS.get(m.status.value, 0)
            status = _c(color, m.status.value.ljust(12))
            inbox_count = self._mailbox.pending_count(m.agent_id)
            inbox_tag = f" 📬{inbox_count}" if inbox_count > 0 else ""
            print(f"    {cyan(m.agent_id[:20].ljust(20))} {m.role[:10].ljust(10)} {status}{inbox_tag}")
        print(f"  {'─'*55}")


# ── 测试场景 ───────────────────────────────────────────────

async def run_scenario(fw, env, monitor: TeamMonitor, event_log: EventLog):
    """执行完整测试场景，monitor 实时追踪。"""

    async def llm(prompt: str, label: str = "LEAD") -> str:
        t0 = time.monotonic()
        event_log.event(label, "LLM_CALL", prompt[:80])
        result = await fw.run(prompt)
        elapsed = time.monotonic() - t0
        answer = result.final_answer or result.error or ""
        event_log.event(label, "LLM_RESPONSE", answer[:80], elapsed=round(elapsed, 1))
        monitor.poll_status()
        return answer

    print(f"\n{'═'*70}")
    print(f"  {bold(magenta('场景: 多 Agent 协作 + 全状态监控'))}")
    print(f"{'═'*70}\n")

    # Phase 1: 创建团队
    print(f"\n  {bold(yellow('── Phase 1: 团队创建 ──'))}")
    await llm(
        "使用 team 工具查看当前团队状态: team(action='status')",
        "LEAD",
    )
    monitor.print_dashboard()

    # Phase 2: Spawn 3 teammates
    print(f"\n  {bold(yellow('── Phase 2: Spawn 3 Teammates ──'))}")
    await llm(
        "依次 spawn 3 个 teammate: "
        "1) team(action='spawn', role='researcher', task='列出Python异步编程的3个核心概念') "
        "2) team(action='spawn', role='coder', task='用Python写一个hello world函数') "
        "3) team(action='spawn', role='tester', task='列出单元测试的3个最佳实践')",
        "LEAD",
    )
    monitor.print_dashboard()

    # Phase 3: 监控执行过程
    print(f"\n  {bold(yellow('── Phase 3: 实时监控执行 ──'))}")
    for i in range(12):
        await asyncio.sleep(2)
        changes = monitor.poll_status()
        if changes:
            for aid, role, prev, curr in changes:
                event_log.event(aid, "STATUS_CHANGE", f"{prev}→{curr}")
        # 每 6 秒打印一次 dashboard
        if i % 3 == 2:
            monitor.print_dashboard()

    # Phase 4: 广播通知
    print(f"\n  {bold(yellow('── Phase 4: Lead 广播 ──'))}")
    await llm(
        "mail(action='broadcast', event_type='BROADCAST_NOTICE', "
        "payload={\"message\": \"所有任务即将截止，请加快进度\"})",
        "LEAD",
    )

    # Phase 5: 发布/订阅
    print(f"\n  {bold(yellow('── Phase 5: 发布/订阅 ──'))}")
    await llm(
        "mail(action='subscribe', topic_pattern='report.*')",
        "LEAD",
    )
    await llm(
        "mail(action='publish', topic='report.progress', "
        "payload={\"overall_progress\": 80, \"blockers\": 0})",
        "LEAD",
    )

    # Phase 6: Collect 结果
    print(f"\n  {bold(yellow('── Phase 6: 收集结果 ──'))}")
    await asyncio.sleep(5)
    answer = await llm("team(action='collect') 收集所有 teammate 结果", "LEAD")
    monitor.print_dashboard()

    # Phase 7: 读取 inbox
    print(f"\n  {bold(yellow('── Phase 7: Lead Inbox ──'))}")
    await llm("mail(action='read') 读取所有消息", "LEAD")

    # Phase 8: Shutdown 全部
    print(f"\n  {bold(yellow('── Phase 8: Team Shutdown ──'))}")
    await llm("team(action='shutdown') 关闭整个团队", "LEAD")
    await asyncio.sleep(3)
    monitor.poll_status()
    monitor.print_dashboard()


# ── 主流程 ───────────────────────────────────────────────

async def main(config_path: str, interval: float, log_file: str | None, verbose: bool) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config, _setup_team

    logging.getLogger("agent_framework").setLevel(
        logging.DEBUG if verbose else logging.WARNING
    )

    print(f"\n{bold('Agent Team 全量监控')}")
    print(f"Config:   {cyan(config_path)}")
    print(f"Interval: {interval}s")
    if log_file:
        print(f"Log:      {cyan(log_file)}")
    print()

    config = load_config(config_path)
    # 增大 quota 以支持多 spawn
    config.subagent.max_sub_agents_per_run = 10
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)
    env = _setup_team(fw, "monitor_team")

    event_log = EventLog(log_file=log_file)
    monitor = TeamMonitor(env, event_log)
    monitor.setup_bus_listener()

    print(f"  {green('✓')} Team: {env['coordinator'].team_id}")
    print(f"  {green('✓')} Model: {config.model.default_model_name}")
    print(f"  {green('✓')} Bus 监听已启动\n")

    try:
        await run_scenario(fw, env, monitor, event_log)
    except KeyboardInterrupt:
        print(f"\n  {yellow('中断')}")
    except Exception as e:
        print(f"\n  {red(f'错误: {e}')}")
        if verbose:
            import traceback
            traceback.print_exc()

    # 最终报告
    event_log.print_timeline()
    event_log.print_state_diagram()

    # 最终统计
    print(f"\n{'═'*70}")
    print(f"  {bold(magenta('统计'))}")
    print(f"{'═'*70}")
    members = env["registry"].list_members()
    status_counts = defaultdict(int)
    for m in members:
        status_counts[m.status.value] += 1
    print(f"  总成员: {len(members)}")
    for s, c in sorted(status_counts.items()):
        color = STATUS_COLORS.get(s, 0)
        print(f"    {_c(color, s)}: {c}")
    print(f"  总线事件: {monitor._bus_event_count}")
    print(f"  事件日志: {len(event_log._entries)} 条")
    print()

    event_log.close()
    try:
        await fw.shutdown()
        env["bus"].shutdown()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Team 全量状态监控")
    parser.add_argument("--config", default="config/doubao.local.json")
    parser.add_argument("--interval", type=float, default=2.0, help="状态轮询间隔(秒)")
    parser.add_argument("--log", default=None, help="事件日志输出文件")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"{red('Error')}: {args.config} 不存在")
        sys.exit(1)

    sys.exit(asyncio.run(main(args.config, args.interval, args.log, args.verbose)))

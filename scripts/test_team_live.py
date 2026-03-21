#!/usr/bin/env python3
"""Live Team 调试脚本 — 用真实 LLM 验证 Team 交互闭环。

使用方式:
    python scripts/test_team_live.py
    python scripts/test_team_live.py --config config/doubao.local.json
    python scripts/test_team_live.py --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# 确保项目根目录在 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── 颜色输出 ───────────────────────────────────────────────

def _c(code: int, text: str) -> str:
    return f"\033[{code}m{text}\033[0m"

def green(t: str) -> str: return _c(32, t)
def red(t: str) -> str: return _c(31, t)
def yellow(t: str) -> str: return _c(33, t)
def cyan(t: str) -> str: return _c(36, t)
def dim(t: str) -> str: return _c(2, t)
def bold(t: str) -> str: return _c(1, t)


def log(label: str, msg: str, color: int = 0) -> None:
    ts = time.strftime("%H:%M:%S")
    prefix = _c(color, f"[{label}]") if color else f"[{label}]"
    print(f"  {dim(ts)} {prefix} {msg}")


def section(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {bold(yellow(title))}")
    print(f"{'='*60}")


def passed(name: str) -> None:
    print(f"  {green('✓')} {name}")


def failed(name: str, error: str) -> None:
    print(f"  {red('✗')} {name}: {error}")


# ── 框架初始化 ───────────────────────────────────────────────

def init_framework(config_path: str, verbose: bool = False):
    """初始化框架 + Team 环境。"""
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.infra.config import FrameworkConfig
    from agent_framework.terminal_runtime import load_config, _setup_team

    level = logging.DEBUG if verbose else logging.WARNING
    logging.getLogger("agent_framework").setLevel(level)

    config = load_config(config_path)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    # 启动 Team
    team_env = _setup_team(fw, "live_test_team")
    coordinator = team_env["coordinator"]
    mailbox = team_env["mailbox"]
    registry = team_env["registry"]
    bus = team_env["bus"]

    return fw, coordinator, mailbox, registry, bus


# ── 测试用例 ───────────────────────────────────────────────

async def test_1_single_agent_task(fw, coordinator, mailbox, verbose):
    """测试 1: Lead 通过 framework.run() 直接调用 team 工具。"""
    section("测试 1: Lead 调用 team(action='status')")

    result = await fw.run("调用 team 工具查看团队状态，使用 team(action='status')")
    answer = result.final_answer or ""
    log("LLM", f"回复: {answer[:200]}", 36)

    if result.success:
        passed("framework.run 成功")
    else:
        failed("framework.run", result.error or "unknown")

    return result.success


async def test_2_spawn_teammate(fw, coordinator, mailbox, registry, verbose):
    """测试 2: Lead spawn 一个 teammate 执行简单任务。"""
    section("测试 2: Spawn Teammate + 收集结果")

    log("LEAD", "发起 spawn 请求...", 33)
    result = await fw.run(
        "使用 team 工具创建一个 teammate 来回答问题'1+1等于几'。"
        "步骤: 1) team(action='spawn', role='math', task='回答1+1等于几') "
        "2) 等 5 秒 3) team(action='collect') 收集结果"
    )
    answer = result.final_answer or ""
    log("LLM", f"回复: {answer[:300]}", 36)

    # 检查 registry 中是否有 teammate
    members = registry.list_members()
    teammate_count = sum(1 for m in members if m.role != "lead")
    log("CHECK", f"团队成员数: {len(members)}, Teammates: {teammate_count}", 34)

    if teammate_count > 0:
        passed(f"Teammate 已 spawn ({teammate_count} 个)")
    else:
        failed("Teammate spawn", "Registry 中无 teammate")

    # 等待后台 _watch_teammate 收集结果
    log("WAIT", "等待 10s 让后台任务完成...", 2)
    await asyncio.sleep(10)

    # 检查 mailbox 是否有结果
    lead_id = None
    for m in members:
        if m.role == "lead":
            lead_id = m.agent_id
            break

    if lead_id:
        events = mailbox.read_inbox(lead_id)
        log("INBOX", f"Lead 收件箱: {len(events)} 条消息", 34)
        for evt in events:
            log("EVENT", f"  type={evt.event_type.value} from={evt.from_agent} payload={str(evt.payload)[:150]}", 2)
        if events:
            passed("结果已回传到 Lead inbox")
        else:
            log("INFO", "inbox 为空，可能结果已被 LLM 消费", 2)

    return result.success


async def test_3_mail_send(fw, coordinator, mailbox, registry, verbose):
    """测试 3: Lead 通过 mail 工具发送消息。"""
    section("测试 3: mail(action='send') 发送消息")

    result = await fw.run(
        "使用 mail 工具广播一条消息给所有团队成员: "
        "mail(action='broadcast', event_type='BROADCAST_NOTICE', "
        "payload={\"message\": \"hello team\"})"
    )
    answer = result.final_answer or ""
    log("LLM", f"回复: {answer[:200]}", 36)

    if result.success:
        passed("mail broadcast 调用成功")
    else:
        failed("mail broadcast", result.error or "unknown")

    return result.success


async def test_4_multi_turn_team(fw, coordinator, mailbox, registry, verbose):
    """测试 4: 多轮 Team 交互 — spawn → status → collect。"""
    section("测试 4: 多轮 Team 交互")

    turns = [
        ("查看当前团队状态，调用 team(action='status')", "status"),
        ("创建一个 teammate 写一首两行诗: team(action='spawn', role='poet', task='写一首两行的诗')", "spawn"),
        ("等待 8 秒后收集结果: team(action='collect')", "collect"),
    ]

    for i, (prompt, label) in enumerate(turns, 1):
        log(f"TURN {i}", prompt[:80], 33)
        result = await fw.run(prompt)
        answer = result.final_answer or ""
        log("LLM", f"回复: {answer[:200]}", 36)

        if label == "spawn":
            # 等待子代理完成
            await asyncio.sleep(8)

        if result.success:
            passed(f"Turn {i} ({label})")
        else:
            failed(f"Turn {i} ({label})", result.error or "unknown")

    return True


async def test_5_team_status_check(fw, coordinator, mailbox, registry, verbose):
    """测试 5: 直接调用 coordinator 检查状态一致性。"""
    section("测试 5: 状态一致性检查")

    status = coordinator.get_team_status()
    log("STATUS", json.dumps(status, indent=2, ensure_ascii=False), 34)

    members = registry.list_members()
    for m in members:
        log("MEMBER", f"{m.agent_id} role={m.role} status={m.status.value}", 2)

    passed(f"团队 {status['team_id']}: {status['member_count']} 成员")
    return True


# ── 主流程 ───────────────────────────────────────────────

async def main(config_path: str, verbose: bool = False) -> int:
    print(f"\n{bold('Agent Team Live Test')}")
    print(f"Config: {cyan(config_path)}")
    print(f"Verbose: {verbose}\n")

    try:
        fw, coordinator, mailbox, registry, bus = init_framework(config_path, verbose)
    except Exception as e:
        failed("框架初始化", str(e))
        import traceback
        traceback.print_exc()
        return 1

    log("INIT", f"框架已初始化, Team: {coordinator.team_id}", 32)
    log("INIT", f"模型: {fw.config.model.default_model_name}", 32)

    results = []
    tests = [
        ("单 Agent 调用 team 工具", test_1_single_agent_task, (fw, coordinator, mailbox, verbose)),
        ("Spawn Teammate + 收集", test_2_spawn_teammate, (fw, coordinator, mailbox, registry, verbose)),
        ("Mail 广播", test_3_mail_send, (fw, coordinator, mailbox, registry, verbose)),
        ("多轮 Team 交互", test_4_multi_turn_team, (fw, coordinator, mailbox, registry, verbose)),
        ("状态一致性", test_5_team_status_check, (fw, coordinator, mailbox, registry, verbose)),
    ]

    for name, test_fn, args in tests:
        try:
            ok = await test_fn(*args)
            results.append((name, ok, None))
        except Exception as e:
            results.append((name, False, str(e)))
            failed(name, str(e))
            if verbose:
                import traceback
                traceback.print_exc()

    # 汇总
    section("测试结果")
    total = len(results)
    ok_count = sum(1 for _, ok, _ in results if ok)
    for name, ok, err in results:
        if ok:
            passed(name)
        else:
            failed(name, err or "failed")

    print(f"\n  {bold(f'{ok_count}/{total} passed')}")

    # Cleanup
    try:
        await fw.shutdown()
        bus.shutdown()
    except Exception:
        pass

    return 0 if ok_count == total else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Agent Team 真实 LLM 调试脚本")
    parser.add_argument("--config", default="config/doubao.local.json", help="配置文件路径")
    parser.add_argument("--verbose", "-v", action="store_true", help="显示详细日志")
    args = parser.parse_args()

    if not Path(args.config).exists():
        print(f"{red('Error')}: 配置文件不存在: {args.config}")
        sys.exit(1)

    sys.exit(asyncio.run(main(args.config, args.verbose)))

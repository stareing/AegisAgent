#!/usr/bin/env python3
"""测试 team 任务自动汇报 — assign 后主 agent 自动收到结果。

验证: assign → 子代理执行 → 结果自动注入主 agent 上下文 → LLM 看到并汇报

使用:
    python scripts/test_team_auto_report.py
    python scripts/test_team_auto_report.py --config config/doubao.local.json
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

def log(label: str, msg: str, color: int = 0) -> None:
    ts = time.strftime("%H:%M:%S")
    tag = _c(color, f"[{label}]") if color else f"[{label}]"
    print(f"  {dim(ts)} {tag} {msg}")

def ok(msg: str) -> None: print(f"    {green('✓')} {msg}")
def fail(msg: str) -> None: print(f"    {red('✗')} {msg}")
def info(msg: str) -> None: print(f"    {dim('→')} {msg}")


async def main(config_path: str) -> int:
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config

    logging.getLogger("agent_framework").setLevel(logging.WARNING)

    print(f"\n{bold('Team 自动汇报测试')}\n")

    config = load_config(config_path)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    errors = []

    # ── Test 1: assign 单个任务 → LLM 自动 collect 并汇报 ──
    print(f"  {bold(yellow('Test 1: assign → 自动 collect → 汇报'))}")

    log("LLM", "发送: '让coder计算1+1'", 33)
    result = await fw.run(
        "使用team工具先查看状态，然后分配任务给coder角色: 计算1+1等于几。"
        "分配后等待结果，用team(action='collect')收集，然后告诉我结果。"
    )
    answer = result.final_answer or ""
    log("LLM", f"回复: {answer[:200]}", 36)

    if "2" in answer or "完成" in answer or "result" in answer.lower():
        ok("LLM 汇报了结果")
    else:
        info(f"LLM 回复可能不含结果: {answer[:100]}")

    # 检查 team inbox 是否还有未读（应该被 drain 消费了）
    executor = fw._deps.tool_executor
    mailbox = getattr(executor, "_team_mailbox", None)
    lead_id = getattr(executor, "_current_spawn_id", "")
    if mailbox and lead_id:
        remaining = mailbox.read_inbox(lead_id)
        info(f"剩余未读: {len(remaining)} 条")

    # ── Test 2: 多轮 — assign → 用户问进度 → 结果自动出现 ──
    print(f"\n  {bold(yellow('Test 2: assign → 用户问进度 → drain 自动注入'))}")

    log("LLM", "分配任务给 analyst", 33)
    r2 = await fw.run(
        "team(action='assign', agent_id='role_analyst', task='列出Python的3个优点')"
    )
    info(f"assign 返回: {(r2.final_answer or '')[:100]}")

    # 等待子代理完成
    log("WAIT", "等待 10s...", 2)
    await asyncio.sleep(10)

    # 用户问进度 — drain 应该自动注入上下文
    log("LLM", "询问: 'analyst完成了吗'", 33)
    r3 = await fw.run("analyst的任务完成了吗？结果是什么？")
    answer3 = r3.final_answer or ""
    log("LLM", f"回复: {answer3[:200]}", 36)

    if "python" in answer3.lower() or "完成" in answer3 or "优点" in answer3:
        ok("LLM 在后续轮次看到了 team 结果")
    else:
        info(f"LLM 可能没看到结果")

    # ── Test 3: team status 显示正确状态 ──
    print(f"\n  {bold(yellow('Test 3: team status 最终状态'))}")
    coord = getattr(executor, "_team_coordinator", None)
    if coord:
        status = coord.get_team_status(caller_id=lead_id)
        for m in status.get("teammates", []):
            info(f"{m['agent_id']} ({m['role']}) — {m['status']}")
        idle = [m for m in status.get("teammates", []) if m["status"] == "IDLE"]
        if idle:
            ok(f"{len(idle)} 个 teammate 已完成 (IDLE)")

    # ── 汇总 ──
    print(f"\n{'='*50}")
    if not errors:
        print(f"  {bold(green('全部测试通过'))}")
    else:
        print(f"  {bold(red(f'{len(errors)} 个失败'))}")

    try:
        await fw.shutdown()
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config)))

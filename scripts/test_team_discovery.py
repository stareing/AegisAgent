#!/usr/bin/env python3
"""真实 TEAM.md 发现 + 注册 + 运行测试。

验证完整链路:
  .agent-team/<role>/TEAM.md → discover → register → spawn → 执行 → 汇报

使用:
    python scripts/test_team_discovery.py
    python scripts/test_team_discovery.py --config config/doubao.local.json -v
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


async def main(config_path: str, verbose: bool) -> int:
    import argparse
    import logging
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config, _setup_team
    from agent_framework.team.loader import discover_teams

    logging.getLogger("agent_framework").setLevel(
        logging.DEBUG if verbose else logging.WARNING
    )

    print(f"\n{bold('TEAM.md 发现 + 注册 + 运行测试')}")
    print(f"Config: {cyan(config_path)}\n")

    errors = []

    # ── Step 1: 发现 ──
    print(f"  {bold(yellow('Step 1: TEAM.md 发现'))}")
    team_dirs = [Path(".agent-team")]
    discovered = discover_teams(team_dirs)
    if discovered:
        ok(f"发现 {len(discovered)} 个角色")
        for t in discovered:
            fm = t["frontmatter"]
            tools = fm.get("allowed-tools", [])
            info(f"{t['team_id']}: {fm.get('description', '')[:50]}  tools={tools}")
    else:
        fail("未发现任何 TEAM.md")
        return 1

    # ── Step 2: team/mail 工具强制检查 ──
    print(f"\n  {bold(yellow('Step 2: team/mail 工具强制放开'))}")
    all_have_team_mail = True
    for t in discovered:
        tools = t["frontmatter"].get("allowed-tools", [])
        has_team = "team" in tools
        has_mail = "mail" in tools
        if has_team and has_mail:
            ok(f"{t['team_id']}: team={has_team} mail={has_mail}")
        else:
            fail(f"{t['team_id']}: team={has_team} mail={has_mail}")
            all_have_team_mail = False
            errors.append("forced_tools")
    if all_have_team_mail:
        ok("所有角色都有 team + mail 工具")

    # ── Step 3: 角色唯一性 ──
    print(f"\n  {bold(yellow('Step 3: 角色唯一性'))}")
    role_ids = [t["team_id"] for t in discovered]
    if len(role_ids) == len(set(role_ids)):
        ok(f"无重复: {role_ids}")
    else:
        fail(f"有重复角色: {role_ids}")
        errors.append("uniqueness")

    # ── Step 4: 框架初始化 + Team 启动 ──
    print(f"\n  {bold(yellow('Step 4: 框架初始化 + Team 启动'))}")
    config = load_config(config_path)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    # 检查框架是否发现了 teams
    fw_teams = getattr(fw, "_discovered_teams", [])
    if fw_teams:
        ok(f"框架发现 {len(fw_teams)} 个角色: {[t['team_id'] for t in fw_teams]}")
    else:
        fail("框架未发现 team 定义")
        errors.append("fw_discovery")

    # 启动 team
    team_env = _setup_team(fw, "discovery_test")
    coordinator = team_env["coordinator"]
    mailbox = team_env["mailbox"]
    registry = team_env["registry"]
    ok(f"Team 启动: {coordinator.team_id}, Lead: {coordinator._lead_id}")

    # ── Step 5: 用发现的角色 spawn teammate ──
    print(f"\n  {bold(yellow('Step 5: Spawn 发现的角色'))}")
    # 只 spawn 第一个角色做验证
    first_role = discovered[0]
    role_name = first_role["team_id"]
    role_desc = first_role["frontmatter"].get("description", "execute task")

    log("SPAWN", f"Spawning '{role_name}': {role_desc[:60]}", 33)
    agent_id = await coordinator.spawn_teammate(
        role=role_name,
        task_input=f"你是 {role_name}。简单回答: 1+2+3 等于几？用 mail 工具汇报结果。",
    )
    ok(f"Spawned: {agent_id}")

    member = registry.get(agent_id)
    if member:
        info(f"状态: {member.status.value}, 角色: {member.role}")
    else:
        fail("成员未注册")
        errors.append("register")

    # ── Step 6: 等待执行完成 ──
    print(f"\n  {bold(yellow('Step 6: 等待执行'))}")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        member = registry.get(agent_id)
        if member and member.status.value in ("IDLE", "FAILED"):
            break
        await asyncio.sleep(1)

    final_status = member.status.value if member else "UNKNOWN"
    if final_status == "IDLE":
        ok(f"执行完成: {final_status}")
    elif final_status == "FAILED":
        fail(f"执行失败: {final_status}")
        errors.append("execution")
    else:
        fail(f"超时: {final_status}")
        errors.append("timeout")

    # ── Step 7: 检查 Lead inbox ──
    print(f"\n  {bold(yellow('Step 7: Lead 收件箱'))}")
    lead_inbox = mailbox.read_inbox(coordinator._lead_id)
    log("INBOX", f"Lead 收到 {len(lead_inbox)} 条消息", 34)
    progress_found = False
    for evt in lead_inbox:
        log("EVENT", f"from={evt.from_agent} type={evt.event_type.value} payload={str(evt.payload)[:120]}", 2)
        if evt.event_type.value == "PROGRESS_NOTICE":
            progress_found = True

    if progress_found:
        ok("收到 PROGRESS_NOTICE — 闭环完成")
    elif lead_inbox:
        ok(f"收到 {len(lead_inbox)} 条消息")
    else:
        fail("Lead inbox 为空")
        errors.append("inbox")

    # ── Step 8: 注册第二个角色验证唯一性运行时 ──
    print(f"\n  {bold(yellow('Step 8: 角色唯一性运行时检查'))}")
    if len(discovered) > 1:
        second_role = discovered[1]["team_id"]
        try:
            agent_id_2 = await coordinator.spawn_teammate(
                role=second_role,
                task_input="简单回答: hello",
            )
            ok(f"第二角色 '{second_role}' spawned: {agent_id_2}")

            # 再次 spawn 同一角色应该失败
            try:
                await coordinator.spawn_teammate(role=second_role, task_input="dup")
                fail(f"重复角色 '{second_role}' 被接受 (BUG)")
                errors.append("dup_role")
            except ValueError as e:
                ok(f"重复角色被拒绝: {str(e)[:60]}")
        except Exception as e:
            info(f"第二角色 spawn 异常: {e}")
    else:
        info("只有 1 个角色，跳过")

    # ── 汇总 ──
    print(f"\n{'='*55}")
    total_checks = 8
    passed = total_checks - len(errors)
    print(f"  {bold(f'{passed}/{total_checks} passed')}")
    if errors:
        print(f"  失败项: {', '.join(errors)}")

    try:
        await fw.shutdown()
        team_env["bus"].shutdown()
    except Exception:
        pass

    return 0 if not errors else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/doubao.local.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.config, args.verbose)))

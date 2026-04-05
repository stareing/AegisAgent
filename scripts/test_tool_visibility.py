#!/usr/bin/env python3
"""工具可见性审查 — 验证 team member vs 普通 sub-agent 的工具隔离。

验证：
  1. Lead 可见所有工具（含 team/mail/spawn）
  2. Team member 可见：allowed-tools + team/mail，不可见：spawn/system/network
  3. 普通 sub-agent 不可见：team/mail/spawn/system/network
  4. TEAM.md allowed-tools 白名单生效

使用:
    python scripts/test_tool_visibility.py
    python scripts/test_tool_visibility.py --config config/doubao.local.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

def _c(code, t): return f"\033[{code}m{t}\033[0m"
green = lambda t: _c(32, t)
red = lambda t: _c(31, t)
cyan = lambda t: _c(36, t)
dim = lambda t: _c(2, t)
bold = lambda t: _c(1, t)
magenta = lambda t: _c(35, t)

results = []
def ok(msg): print(f"    {green('✓')} {msg}"); results.append((msg, True))
def fail(msg): print(f"    {red('✗')} {msg}"); results.append((msg, False))
def info(msg): print(f"    {dim('→')} {msg}")
def section(t): print(f"\n{'━'*60}\n  {bold(magenta(t))}\n{'━'*60}")


def main(config_path: str) -> int:
    from agent_framework.entry import AgentFramework
    from agent_framework.terminal_runtime import load_config
    from agent_framework.subagent.factory import SubAgentFactory
    from agent_framework.models.subagent import SubAgentSpec, SpawnMode

    print(f"\n{bold('工具可见性审查')}\n")

    config = load_config(config_path)
    fw = AgentFramework(config=config)
    fw.setup(auto_approve_tools=True)

    executor = fw._deps.tool_executor
    coordinator = getattr(executor, "_team_coordinator", None)
    factory = SubAgentFactory(fw._deps)

    # ═══════════════════════════════════════════════════════════
    section("1. Lead 工具可见性")
    # ═══════════════════════════════════════════════════════════
    lead_tools = [t.meta.name for t in fw._deps.tool_registry.list_tools()]
    lead_categories = {t.meta.category for t in fw._deps.tool_registry.list_tools()}
    info(f"Lead 工具数: {len(lead_tools)}")
    info(f"Lead 类别: {sorted(lead_categories)}")

    if "team" in lead_tools:
        ok("Lead 可见 team 工具")
    else:
        fail("Lead 不可见 team")
    if "mail" in lead_tools:
        ok("Lead 可见 mail 工具")
    else:
        fail("Lead 不可见 mail")
    if "spawn_agent" in lead_tools:
        ok("Lead 可见 spawn_agent")
    else:
        fail("Lead 不可见 spawn_agent")

    # ═══════════════════════════════════════════════════════════
    section("2. Team Member 工具可见性")
    # ═══════════════════════════════════════════════════════════
    team_id = coordinator.team_id if coordinator else "test_team"
    team_spec = SubAgentSpec(
        parent_run_id=team_id,  # 关键：parent_run_id = team_id → team spawn
        spawn_id="role_coder",
        task_input="test",
        mode=SpawnMode.EPHEMERAL,
        tool_name_whitelist=["read_file", "write_file", "edit_file"],
    )
    _, team_deps = factory.create_agent_and_deps(team_spec, fw._agent)
    team_tools = [t.meta.name for t in team_deps.tool_registry.list_tools()]
    team_categories = {t.meta.category for t in team_deps.tool_registry.list_tools()}

    info(f"Team member 工具数: {len(team_tools)}")
    info(f"Team member 类别: {sorted(team_categories)}")
    info(f"Team member 工具: {sorted(team_tools)}")

    # Must have team/mail
    if "team" in team_tools:
        ok("Team member 可见 team")
    else:
        fail("Team member 不可见 team（应可见）")
    if "mail" in team_tools:
        ok("Team member 可见 mail")
    else:
        fail("Team member 不可见 mail（应可见）")

    # Must have allowed-tools
    if "read_file" in team_tools:
        ok("Team member 可见 read_file (allowed-tools)")
    else:
        fail("Team member 不可见 read_file")
    if "write_file" in team_tools:
        ok("Team member 可见 write_file (allowed-tools)")
    else:
        fail("Team member 不可见 write_file")

    # Must NOT have spawn/system/network
    if "spawn_agent" not in team_tools:
        ok("Team member 不可见 spawn_agent (delegation blocked)")
    else:
        fail("Team member 可见 spawn_agent（应不可见）")
    if "web_fetch" not in team_tools:
        ok("Team member 不可见 web_fetch (network blocked)")
    else:
        fail("Team member 可见 web_fetch（应不可见）")

    # Verify team context propagated
    te = team_deps.tool_executor
    if getattr(te, "_current_agent_role", "") == "teammate":
        ok("Team member 角色 = teammate")
    else:
        fail(f"角色异常: {getattr(te, '_current_agent_role', '?')}")
    if getattr(te, "_team_coordinator", None) is not None:
        ok("Team member 有 team_coordinator")
    else:
        fail("Team member 无 team_coordinator")

    # ═══════════════════════════════════════════════════════════
    section("3. 普通 Sub-Agent 工具可见性")
    # ═══════════════════════════════════════════════════════════
    regular_spec = SubAgentSpec(
        parent_run_id="regular_run_123",  # 不是 team_id → 普通 sub-agent
        spawn_id="sub_worker",
        task_input="do something",
        mode=SpawnMode.EPHEMERAL,
    )
    _, regular_deps = factory.create_agent_and_deps(regular_spec, fw._agent)
    regular_tools = [t.meta.name for t in regular_deps.tool_registry.list_tools()]
    regular_categories = {t.meta.category for t in regular_deps.tool_registry.list_tools()}

    info(f"普通 sub-agent 工具数: {len(regular_tools)}")
    info(f"普通 sub-agent 类别: {sorted(regular_categories)}")

    # Must NOT have team/mail
    if "team" not in regular_tools:
        ok("普通 sub-agent 不可见 team (team blocked)")
    else:
        fail("普通 sub-agent 可见 team（应不可见）")
    if "mail" not in regular_tools:
        ok("普通 sub-agent 不可见 mail (team blocked)")
    else:
        fail("普通 sub-agent 可见 mail（应不可见）")

    # Must NOT have spawn
    if "spawn_agent" not in regular_tools:
        ok("普通 sub-agent 不可见 spawn_agent (delegation blocked)")
    else:
        fail("普通 sub-agent 可见 spawn_agent")

    # Should have filesystem tools
    if "read_file" in regular_tools:
        ok("普通 sub-agent 可见 read_file (filesystem)")
    else:
        fail("普通 sub-agent 不可见 read_file")

    # Verify NO team context
    rte = regular_deps.tool_executor
    if getattr(rte, "_team_coordinator", None) is None:
        ok("普通 sub-agent 无 team_coordinator")
    else:
        fail("普通 sub-agent 有 team_coordinator（应没有）")
    if getattr(rte, "_current_agent_role", "") != "teammate":
        ok(f"普通 sub-agent 角色 = {getattr(rte, '_current_agent_role', '?')} (非 teammate)")
    else:
        fail("普通 sub-agent 角色 = teammate（应不是）")

    # ═══════════════════════════════════════════════════════════
    section("4. 对比总览")
    # ═══════════════════════════════════════════════════════════
    print(f"\n    {'工具':<20} {'Lead':>6} {'Team':>6} {'Sub':>6}")
    print(f"    {'─'*20} {'─'*6} {'─'*6} {'─'*6}")
    all_names = sorted(set(lead_tools) | set(team_tools) | set(regular_tools))
    for name in all_names:
        l = "✓" if name in lead_tools else "✗"
        t = "✓" if name in team_tools else "✗"
        s = "✓" if name in regular_tools else "✗"
        print(f"    {name:<20} {l:>6} {t:>6} {s:>6}")
    print()

    # ═══════════════════════════════════════════════════════════
    print(f"\n{'═'*60}")
    total = len(results)
    passed = sum(1 for _, f in results if f)
    failed_items = [(n, f) for n, f in results if not f]
    color = green if not failed_items else red
    print(f"  {bold(color(f'{passed}/{total} passed'))}")
    if failed_items:
        for name, _ in failed_items:
            print(f"    {red('✗')} {name}")
    print(f"{'═'*60}\n")
    return 0 if not failed_items else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/doubao.local.json")
    args = parser.parse_args()
    sys.exit(main(args.config))

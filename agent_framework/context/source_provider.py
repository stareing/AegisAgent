from __future__ import annotations

import uuid
from html import escape as _xml_escape
from typing import TYPE_CHECKING, Any

from agent_framework.context.transaction_group import (ToolTransactionGroup,
                                                       TransactionGroupIndex)
from agent_framework.models.memory import MemoryRecord
from agent_framework.models.message import Message

if TYPE_CHECKING:
    from agent_framework.models.agent import AgentConfig, Skill
    from agent_framework.models.session import SessionSnapshot, SessionState


class ContextSourceProvider:
    """Collects and formats context materials from various sources.

    This is where Saved Memory formatting happens (section 12.4).
    The memory layer only returns MemoryRecord lists.

    Format stability contract:
    - Same input MUST produce identical output (deterministic).
    - Memory display order: pinned first, then by kind, then alphabetical title.
    - Same memory kind uses a fixed template — no per-call variation.
    - Saved Memories block always uses role="system" in slot 3.
    - No randomness, no time-dependent formatting, no caller-dependent branching.

    Transaction group consumption (v2.6.3 §40):
    - When a TransactionGroupIndex is provided, MUST consume it as-is.
    - MUST NOT reconstruct groups from linear message order.
    - MUST NOT generate new transaction_group_id values.
    - Missing metadata → degrade to "not safely trimmable", NOT rebuild.
    """

    def collect_system_core(
        self,
        agent_config: AgentConfig,
        runtime_info: dict | None = None,
        tool_entries: list | None = None,
    ) -> str:
        """Build the core system prompt with XML structure.

        Output format:
        <system-identity>...</system-identity>
        <runtime-environment>...</runtime-environment>
        <agent-capabilities>...</agent-capabilities>
        <available-tools>
          <local-tools>...</local-tools>
          <mcp-tools>...</mcp-tools>
          <a2a-tools>...</a2a-tools>
        </available-tools>
        """
        parts = [f"<system-identity>\n{agent_config.system_prompt}\n</system-identity>"]

        # Plan mode addon — injected when approval_mode is PLAN
        if runtime_info and runtime_info.get("approval_mode") == "PLAN":
            from agent_framework.agent.prompt_templates import PLAN_MODE_ADDON
            parts.append(
                f"<plan-mode-active>\n{PLAN_MODE_ADDON}\n</plan-mode-active>"
            )

        if runtime_info:
            # Split into environment vs capabilities
            env_keys = {"operating_system", "working_directory"}
            cap_keys = {
                "can_spawn_subagents", "parallel_tool_calls",
                "max_iterations", "max_concurrent_subagents", "max_subagents_per_run",
                "current_iteration", "spawned_subagents",
            }
            meta_keys = {"investigation_mode", "investigation_expectation"}
            todo_keys = {"todo_summary", "todo_reminder"}

            env_lines = [
                f"  <{k}>{_xml_escape(str(v))}</{k}>"
                for k, v in runtime_info.items() if k in env_keys
            ]
            cap_lines = [
                f"  <{k}>{_xml_escape(str(v))}</{k}>"
                for k, v in runtime_info.items() if k in cap_keys
            ]
            other_lines = [
                f"  <{k}>{_xml_escape(str(v))}</{k}>"
                for k, v in runtime_info.items()
                if k not in env_keys and k not in cap_keys and k not in meta_keys and k not in todo_keys
            ]

            if env_lines or other_lines:
                parts.append(
                    "<runtime-environment>\n"
                    + "\n".join(env_lines + other_lines)
                    + "\n</runtime-environment>"
                )
            if cap_lines:
                parts.append(
                    "<agent-capabilities>\n"
                    + "\n".join(cap_lines)
                    + "\n</agent-capabilities>"
                )
            if runtime_info.get("investigation_mode") == "codebase_analysis":
                expectation = _xml_escape(str(runtime_info.get("investigation_expectation", "")))
                parts.append(
                    "<investigation-protocol type=\"codebase-analysis\">\n"
                    "  <required>true</required>\n"
                    "  <rules>\n"
                    "    <rule>Start with code search / file discovery before summarizing architecture.</rule>\n"
                    "    <rule>Inspect multiple implementation files, not only entrypoints or __init__ files.</rule>\n"
                    "    <rule>Cross-check at least entry, runtime, and one downstream subsystem relevant to the question.</rule>\n"
                    "    <rule>In the final answer, clearly separate verified facts from inferences.</rule>\n"
                    f"    <rule>{expectation}</rule>\n"
                    "  </rules>\n"
                    "</investigation-protocol>"
                )
            # Todo state block (rendered via runtime_info, not user messages)
            todo_summary = runtime_info.get("todo_summary")
            todo_reminder = runtime_info.get("todo_reminder")
            if todo_summary or todo_reminder:
                todo_lines = ["<todo-state>"]
                if todo_summary:
                    todo_lines.append(f"  <summary>{_xml_escape(todo_summary)}</summary>")
                if todo_reminder:
                    todo_lines.append(f"  <reminder>{_xml_escape(todo_reminder)}</reminder>")
                todo_lines.append("</todo-state>")
                parts.append("\n".join(todo_lines))

        # Tool catalog with source-based XML grouping
        if tool_entries:
            parts.append(self._format_tool_catalog(tool_entries))

        return "\n\n".join(parts)

    @staticmethod
    def _format_tool_catalog(tool_entries: list) -> str:
        """Format tool entries grouped by source (local/mcp/a2a) with XML tags."""
        groups: dict[str, list] = {}
        for entry in tool_entries:
            source = getattr(entry.meta, "source", "local") or "local"
            groups.setdefault(source, []).append(entry)

        lines = ["<available-tools>"]
        source_order = ["local", "mcp", "a2a", "subagent"]
        for source in source_order:
            entries = groups.pop(source, [])
            if not entries:
                continue
            tag = f"{source}-tools"
            server_attr = ""
            if source == "mcp" and entries:
                servers = sorted({getattr(e.meta, "mcp_server_id", "") for e in entries})
                server_attr = f' servers="{_xml_escape(",".join(servers))}"'
            elif source == "a2a" and entries:
                urls = sorted({getattr(e.meta, "a2a_agent_url", "") for e in entries})
                server_attr = f' agents="{_xml_escape(",".join(urls))}"'
            lines.append(f"  <{tag}{server_attr}>")
            for entry in sorted(entries, key=lambda e: e.meta.name):
                desc = _xml_escape((entry.meta.description or "")[:80])
                lines.append(f"    <tool name=\"{_xml_escape(entry.meta.name)}\">{desc}</tool>")
            lines.append(f"  </{tag}>")
        # Any remaining sources
        for source, entries in sorted(groups.items()):
            tag = f"{source}-tools"
            lines.append(f"  <{tag}>")
            for entry in sorted(entries, key=lambda e: e.meta.name):
                desc = _xml_escape((entry.meta.description or "")[:80])
                lines.append(f"    <tool name=\"{_xml_escape(entry.meta.name)}\">{desc}</tool>")
            lines.append(f"  </{tag}>")
        lines.append("</available-tools>")
        return "\n".join(lines)

    def collect_skill_addon(self, active_skill: Skill | None) -> str | None:
        """Get skill-specific system prompt addon, wrapped in XML."""
        if active_skill and active_skill.system_prompt_addon:
            esc_id = _xml_escape(active_skill.skill_id)
            esc_name = _xml_escape(active_skill.name)
            return (
                f"<active-skill id=\"{esc_id}\" name=\"{esc_name}\">\n"
                f"{active_skill.system_prompt_addon}\n"
                f"</active-skill>"
            )
        return None

    def collect_saved_memory_block(
        self, records: list[MemoryRecord]
    ) -> str | None:
        """Format saved memories into a text block for injection.

        This is where memory formatting happens - memory layer never does this.

        Deterministic ordering: pinned first, then by kind, then alphabetical title.
        Same input always produces identical output.
        """
        if not records:
            return None

        # Stable sort: pinned first → kind → title (alphabetical)
        sorted_records = sorted(
            records,
            key=lambda r: (not r.is_pinned, r.kind.value, r.title.lower()),
        )

        lines = ["<saved-memories>"]
        for r in sorted_records:
            pinned_attr = ' pinned="true"' if r.is_pinned else ""
            esc_tags = _xml_escape(",".join(sorted(r.tags)))
            tags_attr = f' tags="{esc_tags}"' if r.tags else ""
            lines.append(f'  <memory kind="{_xml_escape(r.kind.value)}"{pinned_attr}{tags_attr}>')
            lines.append(f"    <title>{_xml_escape(r.title)}</title>")
            lines.append(f"    <content>{_xml_escape(r.content)}</content>")
            lines.append("  </memory>")
        lines.append("</saved-memories>")
        return "\n".join(lines)

    def collect_session_groups(
        self,
        session_state: SessionState | SessionSnapshot,
        transaction_index: TransactionGroupIndex | None = None,
    ) -> list[ToolTransactionGroup]:
        """Organize session messages into transaction groups.

        v2.6.3 §40: When a TransactionGroupIndex is provided, consume it
        directly. Do NOT reconstruct groups from linear message order.

        v2.6.4 §45: Prefers SessionSnapshot (read-only) over mutable
        SessionState. When SessionSnapshot is passed, the context layer
        sees a frozen view that cannot change mid-build.
        """
        # Transcript repair: fix malformed tool calls before building context
        from agent_framework.models.transcript_repair import repair_session_messages
        try:
            raw_msgs = [m.model_dump() for m in session_state.messages]
            repaired = repair_session_messages(raw_msgs)
            # Only apply if repair changed something (avoid unnecessary mutation)
            if len(repaired) != len(raw_msgs):
                session_state.messages = [Message(**m) for m in repaired]
        except Exception:
            pass  # Repair is best-effort; never block context build

        # If pre-computed index is available, use it directly
        if transaction_index is not None:
            return self._consume_transaction_index(transaction_index)

        # Fallback: build groups from linear messages (legacy path)
        return self._build_groups_from_messages(session_state)

    def _consume_transaction_index(
        self, index: TransactionGroupIndex
    ) -> list[ToolTransactionGroup]:
        """Consume pre-computed transaction groups from index.

        Does NOT generate new group_ids or restructure groups.
        """
        # Return groups in iteration order if available, else by id order
        if index.groups_by_iteration:
            result: list[ToolTransactionGroup] = []
            seen: set[str] = set()
            for iter_key in sorted(index.groups_by_iteration.keys()):
                for group_id in index.groups_by_iteration[iter_key]:
                    if group_id not in seen and group_id in index.groups_by_id:
                        result.append(index.groups_by_id[group_id])
                        seen.add(group_id)
            # Add any groups not referenced by iteration
            for group_id, group in index.groups_by_id.items():
                if group_id not in seen:
                    result.append(group)
            return result
        return list(index.groups_by_id.values())

    def _build_groups_from_messages(
        self, session_state: SessionState | SessionSnapshot
    ) -> list[ToolTransactionGroup]:
        """Build transaction groups from linear session messages (legacy fallback).

        This path is used when no TransactionGroupIndex is available.
        Groups built here are marked protected=True (not safely trimmable)
        because their group_ids are ephemeral, not persisted metadata.
        Callers should prefer providing a TransactionGroupIndex.
        """
        messages = session_state.get_messages()
        groups: list[ToolTransactionGroup] = []

        i = 0
        while i < len(messages):
            msg = messages[i]

            if msg.role == "assistant" and msg.tool_calls:
                # Start of a tool transaction group
                group_msgs = [msg]
                expected_ids = {tc.id for tc in msg.tool_calls}
                j = i + 1
                while j < len(messages) and messages[j].role == "tool":
                    group_msgs.append(messages[j])
                    if messages[j].tool_call_id:
                        expected_ids.discard(messages[j].tool_call_id)
                    j += 1

                group_type = "TOOL_BATCH"
                # Check if any tool call is a spawn
                for tc in msg.tool_calls:
                    if "spawn" in tc.function_name:
                        group_type = "SUBAGENT_BATCH"
                        break

                groups.append(
                    ToolTransactionGroup(
                        group_id=str(uuid.uuid4()),
                        group_type=group_type,
                        messages=group_msgs,
                    )
                )
                i = j
            else:
                groups.append(
                    ToolTransactionGroup(
                        group_id=str(uuid.uuid4()),
                        group_type="PLAIN_MESSAGES",
                        messages=[msg],
                    )
                )
                i += 1

        return groups

    def collect_skill_catalog(
        self, skill_descriptions: list[dict[str, str]]
    ) -> str | None:
        """Format available skill descriptions for LLM context injection.

        The LLM sees skill names + descriptions so it can decide when to
        invoke a skill via the invoke_skill tool. Full skill bodies are
        NOT loaded here — only on invocation (lazy loading).
        """
        if not skill_descriptions:
            return None

        lines = [
            '<available-skills hint="Invoke via invoke_skill tool with skill_id">',
        ]
        for desc in skill_descriptions:
            esc_hint = _xml_escape(desc["argument_hint"]) if desc.get("argument_hint") else ""
            hint_attr = f' argument-hint="{esc_hint}"' if esc_hint else ""
            esc_id = _xml_escape(desc["skill_id"])
            esc_name = _xml_escape(desc["name"])
            lines.append(f'  <skill id="{esc_id}" name="{esc_name}"{hint_attr}>')
            lines.append(f"    {_xml_escape(desc['description'])}")
            lines.append("  </skill>")
        lines.append("</available-skills>")
        return "\n".join(lines)

    def collect_session_context(
        self,
        runtime_info: dict | None = None,
    ) -> str | None:
        """Build Gemini-style session context block (environment info).

        Injected as part of the system message to provide the LLM with
        awareness of its execution environment. Contains date, platform,
        workspace, and git status if available.
        """
        import datetime
        import os
        import platform

        lines = ["<session-context>"]

        # Date
        now = datetime.datetime.now()
        date_str = now.strftime("%A, %B %d, %Y")
        lines.append(f"  <date>{_xml_escape(date_str)}</date>")

        # Platform
        os_name = {"Darwin": "macOS", "Windows": "Windows", "Linux": "Linux"}.get(
            platform.system(), platform.system()
        )
        lines.append(f"  <platform>{_xml_escape(os_name)}</platform>")

        # Working directory
        cwd = os.getcwd()
        lines.append(f"  <working_directory>{_xml_escape(cwd)}</working_directory>")

        # Git info (best-effort)
        try:
            import subprocess
            git_branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=3, cwd=cwd,
            )
            if git_branch.returncode == 0:
                branch = git_branch.stdout.strip()
                lines.append(f"  <git_branch>{_xml_escape(branch)}</git_branch>")

                # Recent commits (last 3)
                git_log = subprocess.run(
                    ["git", "log", "--oneline", "-3"],
                    capture_output=True, text=True, timeout=3, cwd=cwd,
                )
                if git_log.returncode == 0 and git_log.stdout.strip():
                    lines.append("  <recent_commits>")
                    for commit_line in git_log.stdout.strip().split("\n")[:3]:
                        lines.append(f"    <commit>{_xml_escape(commit_line)}</commit>")
                    lines.append("  </recent_commits>")
        except Exception:
            pass

        lines.append("</session-context>")
        return "\n".join(lines)

    def collect_current_input(self, task_or_prompt: str) -> Message:
        """Wrap current user input as a Message."""
        return Message(role="user", content=task_or_prompt)

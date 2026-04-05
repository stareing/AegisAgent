"""Fork sub-agent message builders — prompt-cache-friendly child spawning.

Fork children inherit the parent's full conversation context with
byte-identical API prefixes. Only the per-child directive differs,
maximizing prompt cache hits across all forked workers.

Anti-recursion: fork children detect the boilerplate tag and refuse
to spawn further forks.
"""

from __future__ import annotations

from agent_framework.models.message import Message, ToolCallRequest

FORK_PLACEHOLDER_RESULT = "Fork started — processing in background"
FORK_BOILERPLATE_TAG = "fork-worker-context"
FORK_DIRECTIVE_PREFIX = "Your directive:\n"


def build_fork_child_messages(
    parent_assistant_msg: Message,
    directive: str,
) -> list[Message]:
    """Build fork child messages with byte-identical prefix + unique directive.

    The parent assistant message (with tool_calls) is preserved as-is.
    A single user message follows with:
    - Placeholder tool_result for each tool_call (identical across all children)
    - The per-child directive as the final content section

    This structure ensures all fork children share the same API prefix,
    maximizing prompt cache hits.

    Args:
        parent_assistant_msg: The parent's last assistant message with tool_calls.
        directive: The unique task directive for this child.

    Returns:
        List of [assistant_msg, user_msg] for the child's initial context.
    """
    # Build placeholder tool results for each parent tool_call
    tool_result_parts: list[str] = []
    if parent_assistant_msg.tool_calls:
        for tc in parent_assistant_msg.tool_calls:
            tool_result_parts.append(
                f"[tool_result for {tc.function_name} (id={tc.id})]: "
                f"{FORK_PLACEHOLDER_RESULT}"
            )

    # Build child directive with structured output format
    child_msg = build_child_directive(directive)

    # Combine: placeholder results + directive
    content_parts = tool_result_parts + [child_msg]
    user_content = "\n\n".join(content_parts)

    return [
        parent_assistant_msg,
        Message(role="user", content=user_content),
    ]


def build_child_directive(directive: str) -> str:
    """Build the structured fork worker directive.

    Includes rules for fork workers and the output format specification.
    """
    return f"""<{FORK_BOILERPLATE_TAG}>
You are a forked worker process. You are NOT the main agent.

RULES:
1. Do NOT spawn sub-agents or fork further — execute directly.
2. Do NOT converse, ask questions, or suggest next steps.
3. USE your tools directly: read files, run commands, edit code.
4. If you modify files, commit your changes before reporting.
5. Stay strictly within your directive's scope.
6. Keep your report under 500 words. Be factual and concise.
7. Your response MUST begin with "Scope:". No preamble.

Output format:
  Scope: <echo back your assigned scope in one sentence>
  Result: <the answer or key findings>
  Key files: <relevant file paths>
  Files changed: <list with commit hash — only if you modified files>
  Issues: <list — only if there are issues to flag>
</{FORK_BOILERPLATE_TAG}>

{FORK_DIRECTIVE_PREFIX}{directive}"""


def build_worktree_notice(parent_cwd: str, child_cwd: str) -> str:
    """Build a path translation notice for worktree-isolated fork children.

    Args:
        parent_cwd: The parent agent's working directory.
        child_cwd: The child's worktree working directory.

    Returns:
        Notice string to prepend to the child's context.
    """
    return (
        f"You've inherited conversation context from a parent agent working in "
        f"{parent_cwd}. You are operating in an isolated git worktree at "
        f"{child_cwd} — same repository, same relative file structure, separate "
        f"working copy. Paths in the inherited context refer to the parent's "
        f"working directory; translate them to your worktree root. Re-read files "
        f"before editing if the parent may have modified them."
    )


def is_in_fork_child(messages: list[Message]) -> bool:
    """Detect if current context is inside a fork child (anti-recursion guard).

    Checks for the fork boilerplate tag in user messages.
    Fork children must not spawn further forks.
    """
    tag = f"<{FORK_BOILERPLATE_TAG}>"
    for msg in messages:
        if msg.role == "user" and msg.content and tag in msg.content:
            return True
    return False

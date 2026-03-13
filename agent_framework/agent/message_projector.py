"""MessageProjector — converts structured run results into session messages.

Single responsibility: message format conversion. Does NOT write SessionState
directly — returns message lists for RunStateController to commit.

Projection contract (v2.4 §4):
  1. assistant message with content and/or tool_calls → 1 Message(role=assistant)
  2. Each ToolResult → 1 Message(role=tool), including errors
  3. Subagent results → projected as tool message (DelegationSummary in output)
  4. Order: assistant → tool_1 → tool_2 → ... (strict, matches LLM expectation)
  5. tool errors MUST be projected — never silently dropped

Boundary:
  - Does NOT write SessionState
  - Does NOT execute tools
  - Does NOT call the model
  - Does NOT decide authorization
  - Does NOT extract memory
"""
from __future__ import annotations

from agent_framework.models.agent import IterationResult
from agent_framework.models.message import Message


class MessageProjector:
    """Converts IterationResult into a list of session-safe messages.

    Returns messages for the caller to commit via RunStateController.
    """

    @staticmethod
    def project_iteration(iteration_result: IterationResult) -> list[Message]:
        """Project an IterationResult into a list of session messages.

        Returns messages in strict order: assistant first, then tool results.
        Both successful and failed tool results are projected — errors are
        never silently dropped.

        v2.5.2 §22: Each projected message carries iteration_id in metadata
        to link session messages back to the iteration_history audit trail.
        """
        messages: list[Message] = []
        iter_meta = {"iteration_id": iteration_result.iteration_index}

        # Step 1: assistant message (always project if model responded)
        if iteration_result.model_response:
            resp = iteration_result.model_response
            messages.append(
                Message(
                    role="assistant",
                    content=resp.content,
                    tool_calls=resp.tool_calls if resp.tool_calls else None,
                    metadata=dict(iter_meta),
                )
            )

        # Step 2: tool results — one message per result, preserving order
        for tr in iteration_result.tool_results:
            output_str = str(tr.output) if tr.success else str(tr.error)
            messages.append(
                Message(
                    role="tool",
                    content=output_str,
                    tool_call_id=tr.tool_call_id,
                    name=tr.tool_name,
                    metadata=dict(iter_meta),
                )
            )

        return messages

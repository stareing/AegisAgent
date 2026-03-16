"""HookPayloadFactory — canonical payload schemas for each hook point.

Centralizes payload construction so field names are defined once.
When a hook payload field changes, only this file needs updating.
"""

from __future__ import annotations

from typing import Any


def run_start_payload(task: str, model: str) -> dict[str, Any]:
    return {"task": task[:500], "model": model}


def run_finish_payload(
    success: bool, iterations_used: int, total_tokens: int,
    final_answer_preview: str,
) -> dict[str, Any]:
    return {
        "success": success,
        "iterations_used": iterations_used,
        "total_tokens": total_tokens,
        "final_answer_preview": final_answer_preview[:200],
    }


def run_error_payload(
    error_type: str, error_message: str, iterations_used: int,
) -> dict[str, Any]:
    return {
        "error_type": error_type,
        "error_message": error_message[:500],
        "iterations_used": iterations_used,
    }


def iteration_start_payload(iteration_index: int, context_messages: int) -> dict[str, Any]:
    return {"iteration_index": iteration_index, "context_messages": context_messages}


def iteration_finish_payload(
    iteration_index: int, tool_count: int, tokens: int,
) -> dict[str, Any]:
    return {"iteration_index": iteration_index, "tool_count": tool_count, "tokens": tokens}


def iteration_error_payload(
    iteration_index: int, error_type: str, error_message: str,
) -> dict[str, Any]:
    return {
        "iteration_index": iteration_index,
        "error_type": error_type,
        "error_message": error_message[:500],
    }


def tool_pre_use_payload(
    tool_name: str, tool_call_id: str, arguments: dict,
    tool_tags: list[str], source: str,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "arguments": dict(arguments),
        "tool_tags": list(tool_tags),
        "source": source,
    }


def tool_post_use_payload(
    tool_name: str, tool_call_id: str, success: bool, output_preview: str,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "success": success,
        "output_preview": output_preview[:500],
    }


def tool_error_payload(tool_name: str, error_type: str, error_message: str) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "error_type": error_type,
        "error_message": error_message[:500],
    }


def delegation_pre_payload(
    task_input: str, mode: str, memory_scope: str,
    deadline_ms: int, is_async: bool = False,
) -> dict[str, Any]:
    return {
        "task_input": task_input[:500],
        "mode": mode,
        "memory_scope": memory_scope,
        "deadline_ms": deadline_ms,
        "async": is_async,
    }


def delegation_post_payload(
    spawn_id: str, success: bool, iterations_used: int,
    async_collected: bool = False,
) -> dict[str, Any]:
    return {
        "spawn_id": spawn_id,
        "success": success,
        "iterations_used": iterations_used,
        "async_collected": async_collected,
    }


def delegation_error_payload(
    spawn_id: str, error: str, async_collected: bool = False,
) -> dict[str, Any]:
    return {
        "spawn_id": spawn_id,
        "error": error[:500],
        "async_collected": async_collected,
    }


def memory_pre_record_payload(
    content: str, title: str, tags: list[str], kind: str,
) -> dict[str, Any]:
    return {"content": content, "title": title, "tags": list(tags), "kind": kind}


def memory_post_record_payload(memory_id: str, action: str) -> dict[str, Any]:
    return {"memory_id": memory_id, "action": action}


def context_pre_build_payload(
    task: str, memory_count: int, session_message_count: int,
) -> dict[str, Any]:
    return {
        "task": task[:200],
        "memory_count": memory_count,
        "session_message_count": session_message_count,
    }


def context_post_build_payload(
    total_messages: int, total_tokens: int,
    groups_trimmed: int, prefix_reused: bool,
) -> dict[str, Any]:
    return {
        "total_messages": total_messages,
        "total_tokens": total_tokens,
        "groups_trimmed": groups_trimmed,
        "prefix_reused": prefix_reused,
    }


def config_loaded_payload(adapter_type: str, store_type: str) -> dict[str, Any]:
    return {"model_adapter_type": adapter_type, "memory_store_type": store_type}


def instructions_loaded_payload(skills_loaded: int, tools_registered: int) -> dict[str, Any]:
    return {"skills_loaded": skills_loaded, "tools_registered": tools_registered}


def artifact_produced_payload(artifact: dict, source_tool: str) -> dict[str, Any]:
    return {"artifact": artifact, "source_tool": source_tool}


def artifact_finalize_payload(
    artifact_name: str, artifact_type: str, uri: str,
) -> dict[str, Any]:
    return {"artifact_name": artifact_name, "artifact_type": artifact_type, "uri": uri}

"""Built-in cron scheduling tools — create/list/delete scheduled agent tasks.

Category: scheduling — allows agents to set up recurring tasks.
"""

from __future__ import annotations

from typing import Any

from agent_framework.tools.decorator import tool
from agent_framework.tools.schemas.builtin_args import SYSTEM_NAMESPACE

# Injected at setup time by entry.py
_cron_registry: Any = None


def set_cron_registry(registry: Any) -> None:
    """Bind the cron registry (called by entry.py)."""
    global _cron_registry
    _cron_registry = registry


@tool(
    name="cron_create",
    description=(
        "Create a scheduled recurring task using a cron expression. "
        "Format: minute hour day-of-month month day-of-week. "
        "Example: '0 9 * * 1-5' = 9 AM weekdays."
    ),
    category="scheduling",
    require_confirm=True,
    tags=["cron", "schedule", "recurring"],
    namespace=SYSTEM_NAMESPACE,
    search_hint="create schedule cron recurring job",
)
def cron_create(
    name: str,
    cron_expression: str,
    task_prompt: str,
) -> dict:
    """Create a new cron job.

    Args:
        name: Human-readable name for the job.
        cron_expression: 5-field cron expression.
        task_prompt: The task to execute at each trigger.

    Returns:
        Dict with job_id and schedule info.
    """
    if _cron_registry is None:
        return {"success": False, "error": "Cron scheduler not configured"}

    try:
        job = _cron_registry.create(
            name=name,
            cron_expression=cron_expression,
            task_prompt=task_prompt,
        )
        return {
            "success": True,
            "job_id": job.job_id,
            "name": job.name,
            "cron_expression": job.cron_expression,
            "next_run_at": job.next_run_at,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


@tool(
    name="cron_list",
    description="List all scheduled cron jobs.",
    category="scheduling",
    require_confirm=False,
    tags=["cron", "schedule"],
    namespace=SYSTEM_NAMESPACE,
    is_read_only=True,
    search_hint="list scheduled cron jobs",
)
def cron_list() -> dict:
    """List all cron jobs.

    Returns:
        Dict with list of jobs.
    """
    if _cron_registry is None:
        return {"success": False, "error": "Cron scheduler not configured"}

    jobs = _cron_registry.list_jobs()
    return {
        "success": True,
        "count": len(jobs),
        "jobs": [
            {
                "job_id": j.job_id,
                "name": j.name,
                "cron_expression": j.cron_expression,
                "enabled": j.enabled,
                "last_run_at": j.last_run_at,
                "next_run_at": j.next_run_at,
            }
            for j in jobs
        ],
    }


@tool(
    name="cron_delete",
    description="Delete a scheduled cron job by ID.",
    category="scheduling",
    require_confirm=True,
    tags=["cron", "schedule"],
    namespace=SYSTEM_NAMESPACE,
    is_destructive=True,
    search_hint="delete remove cron job",
)
def cron_delete(job_id: str) -> dict:
    """Delete a cron job.

    Args:
        job_id: The ID of the cron job to delete.

    Returns:
        Dict with success status.
    """
    if _cron_registry is None:
        return {"success": False, "error": "Cron scheduler not configured"}

    deleted = _cron_registry.delete(job_id)
    if deleted:
        return {"success": True, "message": f"Job {job_id} deleted"}
    return {"success": False, "error": f"Job {job_id} not found"}

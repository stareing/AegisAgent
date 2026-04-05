from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, get_type_hints

from pydantic import BaseModel, Field, create_model

from agent_framework.models.tool import ToolMeta


def _build_parameters_model(func: Callable) -> tuple[dict, type[BaseModel] | None]:
    """Build a pydantic model + JSON schema from function signature."""
    sig = inspect.signature(func)
    hints = get_type_hints(func)

    fields: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        annotation = hints.get(name, Any)
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, Field(default=default))

    if not fields:
        return {}, None

    model = create_model(f"{func.__name__}_params", **fields)
    schema = model.model_json_schema()
    return schema, model


def _extract_description(func: Callable, override: str | None) -> str:
    """Extract description from docstring first paragraph or use override."""
    if override:
        return override
    doc = inspect.getdoc(func)
    if not doc:
        return ""
    return doc.split("\n\n")[0].strip()


def tool(
    name: str | None = None,
    description: str | None = None,
    category: str = "general",
    require_confirm: bool = False,
    tags: list[str] | None = None,
    namespace: str | None = None,
    source: str = "local",
    concurrency_class: str = "non_concurrent",
    # --- Claude Code aligned fields (v4.1) ---
    prompt: str = "",
    aliases: list[str] | None = None,
    search_hint: str = "",
    is_read_only: bool = False,
    is_destructive: bool = False,
    should_defer: bool = False,
    always_load: bool = False,
    max_result_chars: int = 250_000,
    activity_description: str = "",
    tool_use_summary_tpl: str = "",
) -> Callable:
    """Decorator to register a function as a tool.

    Usage:
        @tool(category="filesystem")
        def read_file(path: str) -> str:
            '''Read a file from disk.'''
            ...
    """

    def decorator(func: Callable) -> Callable:
        is_async = asyncio.iscoroutinefunction(func)
        tool_name = name or func.__name__
        desc = _extract_description(func, description)
        schema, validator = _build_parameters_model(func)

        meta = ToolMeta(
            name=tool_name,
            description=desc,
            parameters_schema=schema,
            category=category,
            require_confirm=require_confirm,
            is_async=is_async,
            tags=tags or [],
            source=source,
            namespace=namespace,
            concurrency_class=concurrency_class,
            prompt=prompt,
            aliases=aliases or [],
            search_hint=search_hint,
            is_read_only=is_read_only,
            is_destructive=is_destructive,
            should_defer=should_defer,
            always_load=always_load,
            max_result_chars=max_result_chars,
            activity_description=activity_description,
            tool_use_summary_tpl=tool_use_summary_tpl,
        )

        func.__tool_meta__ = meta  # type: ignore[attr-defined]
        func.__tool_validator__ = validator  # type: ignore[attr-defined]
        return func

    return decorator

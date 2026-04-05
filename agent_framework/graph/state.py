"""State channel resolution and reducer logic.

LangGraph-compatible state management:
- Plain fields: last-write-wins (default reducer)
- ``Annotated[type, reducer_fn]``: custom reducer merges old + new values
- ``Annotated[list, operator.add]``: append-only list accumulation

State schema is a ``TypedDict`` subclass. At compile time we introspect
``__annotations__`` to extract per-field reducers from ``Annotated`` metadata.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, get_args, get_origin, get_type_hints

# Annotated origin differs between Python versions
try:
    from typing import Annotated  # 3.11+
except ImportError:
    from typing_extensions import Annotated  # noqa: F811


# ── Reducer extraction ─────────────────────────────────────────────

ReducerFn = Callable[[Any, Any], Any]

# Sentinel for "no reducer — last write wins"
_NO_REDUCER = object()


def extract_reducers(state_schema: type) -> dict[str, ReducerFn | None]:
    """Introspect a TypedDict and return ``{field: reducer | None}``.

    For ``Annotated[T, reducer_fn]`` fields the second metadata arg is used
    as the reducer.  All other fields get ``None`` (last-write-wins).
    """
    reducers: dict[str, ReducerFn | None] = {}
    hints = get_type_hints(state_schema, include_extras=True)
    for field_name, hint in hints.items():
        origin = get_origin(hint)
        if origin is Annotated:
            args = get_args(hint)
            # args[0] is the base type, args[1:] are metadata
            reducer = None
            for meta in args[1:]:
                if callable(meta):
                    reducer = meta
                    break
            reducers[field_name] = reducer
        else:
            reducers[field_name] = None
    return reducers


def get_default_state(state_schema: type) -> dict[str, Any]:
    """Build a default state dict from a TypedDict's annotations.

    Uses ``__annotations__`` defaults if available, otherwise:
    - ``list`` → ``[]``
    - ``dict`` → ``{}``
    - ``int`` / ``float`` → ``0``
    - ``str`` → ``""``
    - ``bool`` → ``False``
    - everything else → ``None``
    """
    defaults: dict[str, Any] = {}
    hints = get_type_hints(state_schema, include_extras=True)

    # Check for class-level defaults defined directly on the TypedDict subclass.
    # We must skip inherited dict methods (like .items, .keys, .values) which
    # TypedDict inherits but are NOT field defaults.
    _DICT_ATTRS = set(dir(dict))
    schema_defaults = {}
    if hasattr(state_schema, "__annotations__"):
        for key in state_schema.__annotations__:
            if key not in _DICT_ATTRS and hasattr(state_schema, key):
                schema_defaults[key] = getattr(state_schema, key)

    for field_name, hint in hints.items():
        if field_name in schema_defaults:
            defaults[field_name] = copy.deepcopy(schema_defaults[field_name])
            continue

        # Unwrap Annotated
        base_type = hint
        origin = get_origin(hint)
        if origin is Annotated:
            base_type = get_args(hint)[0]

        # Infer zero-value from base type
        base_origin = get_origin(base_type)
        if base_type is list or base_origin is list:
            defaults[field_name] = []
        elif base_type is dict or base_origin is dict:
            defaults[field_name] = {}
        elif base_type is int:
            defaults[field_name] = 0
        elif base_type is float:
            defaults[field_name] = 0.0
        elif base_type is str:
            defaults[field_name] = ""
        elif base_type is bool:
            defaults[field_name] = False
        else:
            defaults[field_name] = None

    return defaults


def apply_update(
    current_state: dict[str, Any],
    update: dict[str, Any],
    reducers: dict[str, ReducerFn | None],
) -> dict[str, Any]:
    """Merge a partial update into the current state, respecting reducers.

    Returns a NEW dict — never mutates ``current_state``.
    """
    new_state = dict(current_state)
    for key, value in update.items():
        reducer = reducers.get(key)
        if reducer is not None and key in current_state:
            new_state[key] = reducer(current_state[key], value)
        else:
            # Last-write-wins (or new key)
            new_state[key] = value
    return new_state

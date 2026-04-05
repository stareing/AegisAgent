"""Payload redaction — prevents credential leakage in logs and diagnostics.

Recursively scrubs sensitive fields (API keys, passwords, tokens) from
dicts before logging. Also redacts base64 image data to prevent log bloat.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Field name suffixes that indicate sensitive content
_SENSITIVE_SUFFIXES: frozenset[str] = frozenset({
    "apikey", "api_key", "password", "passwd", "passphrase",
    "secret", "secretkey", "secret_key", "token",
    "authorization", "credential", "private_key",
})

# Token-related fields that are NOT sensitive (prevent false positives)
_SAFE_TOKEN_FIELDS: frozenset[str] = frozenset({
    "token_count", "token_budget", "token_limit", "tokens",
    "max_tokens", "prompt_tokens", "completion_tokens",
    "total_tokens", "token_estimate", "token_usage",
    "max_output_tokens",
})

# Base64 image data pattern
_BASE64_IMAGE_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]{100,}")

_REDACTED = "***"
_MAX_RECURSION_DEPTH = 10


def _normalize_key(key: str) -> str:
    """Normalize a field key for sensitive matching."""
    return re.sub(r"[^a-z0-9_]", "", key.lower())


def is_sensitive_field(key: str) -> bool:
    """Check if a field name indicates sensitive content."""
    normalized = _normalize_key(key)

    # Check safe list first (avoid false positives)
    if normalized in _SAFE_TOKEN_FIELDS:
        return False

    # Check if any suffix matches
    for suffix in _SENSITIVE_SUFFIXES:
        if normalized.endswith(suffix):
            return True

    return False


def redact_payload(data: Any, max_depth: int = _MAX_RECURSION_DEPTH) -> Any:
    """Recursively redact sensitive fields in a payload.

    Returns a new dict/list with sensitive values replaced by '***'.
    Non-dict/list values are returned as-is.
    """
    if max_depth <= 0:
        return data

    if isinstance(data, dict):
        result = {}
        for key, value in data.items():
            if isinstance(key, str) and is_sensitive_field(key):
                result[key] = _REDACTED
            else:
                result[key] = redact_payload(value, max_depth - 1)
        return result

    if isinstance(data, (list, tuple)):
        return type(data)(redact_payload(item, max_depth - 1) for item in data)

    # Redact base64 image data in strings
    if isinstance(data, str) and len(data) > 200:
        if _BASE64_IMAGE_RE.search(data):
            digest = hashlib.sha256(data.encode()).hexdigest()[:8]
            return f"<redacted image data, sha256={digest}, ~{len(data)} bytes>"

    return data


def redact_sensitive_processor(
    logger: Any,
    method_name: str,
    event_dict: dict[str, Any],
) -> dict[str, Any]:
    """Structlog processor that redacts sensitive fields before rendering."""
    return redact_payload(event_dict)

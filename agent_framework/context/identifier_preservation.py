"""Identifier preservation during context compaction.

Prevents loss of UUIDs, IPs, URLs, API tokens, and file paths
during LLM-based summarization. Extracted identifiers are injected
as "MUST preserve" instructions into the compaction prompt.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from pydantic import BaseModel


class PreservedIdentifier(NamedTuple):
    """A single extracted identifier with its category."""

    category: str  # "uuid" | "ip" | "url" | "token" | "path"
    value: str


class IdentifierPreservationConfig(BaseModel):
    """Policy for identifier extraction and verification."""

    model_config = {"frozen": True}

    enabled: bool = True
    preserve_uuids: bool = True
    preserve_ips: bool = True
    preserve_urls: bool = True
    preserve_tokens: bool = True
    preserve_paths: bool = True
    max_identifiers: int = 50  # Cap to avoid bloating the prompt


# Compiled patterns for efficiency
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
_IP_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
_URL_RE = re.compile(r"https?://\S+")
_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_-]{20,}\b")
_PATH_RE = re.compile(r"(?:/[\w.-]+){2,}")


def extract_identifiers(
    text: str,
    config: IdentifierPreservationConfig | None = None,
) -> list[PreservedIdentifier]:
    """Extract all identifiable tokens from text."""
    if config is None:
        config = IdentifierPreservationConfig()
    if not config.enabled:
        return []

    found: list[PreservedIdentifier] = []
    seen: set[str] = set()

    def _add(category: str, pattern: re.Pattern[str]) -> None:
        for match in pattern.finditer(text):
            val = match.group()
            if val not in seen:
                seen.add(val)
                found.append(PreservedIdentifier(category=category, value=val))

    if config.preserve_uuids:
        _add("uuid", _UUID_RE)
    if config.preserve_ips:
        _add("ip", _IP_RE)
    if config.preserve_urls:
        _add("url", _URL_RE)
    if config.preserve_paths:
        _add("path", _PATH_RE)
    if config.preserve_tokens:
        _add("token", _TOKEN_RE)

    # Deduplicate: tokens that are substrings of URLs or paths
    urls_and_paths = {i.value for i in found if i.category in ("url", "path")}
    filtered = [
        i
        for i in found
        if i.category not in ("token",)
        or not any(i.value in container for container in urls_and_paths)
    ]

    return filtered[: config.max_identifiers]


def build_preservation_instructions(
    identifiers: list[PreservedIdentifier],
) -> str:
    """Build LLM instruction text for identifier preservation."""
    if not identifiers:
        return ""

    lines = [
        "IMPORTANT: You MUST preserve the following identifiers exactly as-is in your summary:"
    ]
    by_category: dict[str, list[str]] = {}
    for ident in identifiers:
        by_category.setdefault(ident.category, []).append(ident.value)

    category_labels = {
        "uuid": "UUIDs",
        "ip": "IP addresses",
        "url": "URLs",
        "token": "Tokens/Keys",
        "path": "File paths",
    }

    for cat, values in by_category.items():
        label = category_labels.get(cat, cat)
        lines.append(f"  {label}: {', '.join(values[:10])}")
        if len(values) > 10:
            lines.append(f"    ... and {len(values) - 10} more")

    return "\n".join(lines)


def verify_preservation(
    identifiers: list[PreservedIdentifier],
    compressed_text: str,
) -> list[PreservedIdentifier]:
    """Return identifiers that were lost during compression."""
    return [i for i in identifiers if i.value not in compressed_text]

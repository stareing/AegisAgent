"""Model ID normalization — vendor-specific alias mapping.

Normalizes model IDs with version suffixes, preview variants,
and provider-specific naming conventions to canonical forms.
"""

from __future__ import annotations

import re

# Anthropic: strip date suffixes (claude-3-opus-20240229 -> claude-3-opus)
_ANTHROPIC_DATE_RE = re.compile(r"^(claude-[\w-]+?)-\d{8}$")

# Google: normalize preview variants
_GOOGLE_PREVIEW_RE = re.compile(r"^(gemini-[\d.]+-\w+)-preview.*$")

# OpenAI: common aliases
_OPENAI_ALIASES: dict[str, str] = {
    "gpt4": "gpt-4",
    "gpt4o": "gpt-4o",
    "gpt-4-vision": "gpt-4-turbo",
    "gpt-4-vision-preview": "gpt-4-turbo",
    "gpt-3.5": "gpt-3.5-turbo",
    "chatgpt": "gpt-4o",
}

# DeepSeek aliases
_DEEPSEEK_ALIASES: dict[str, str] = {
    "deepseek-v3": "deepseek-chat",
    "deepseek-r1": "deepseek-reasoner",
}


def normalize_model_id(raw_id: str, provider: str = "") -> str:
    """Normalize a model ID to its canonical form.

    Applies vendor-specific rules based on provider hint or ID prefix.
    """
    if not raw_id:
        return raw_id

    model_id = raw_id.strip().lower()

    # Anthropic normalization
    if provider == "anthropic" or model_id.startswith("claude"):
        match = _ANTHROPIC_DATE_RE.match(model_id)
        if match:
            return match.group(1)

    # Google normalization
    if provider == "google" or model_id.startswith("gemini"):
        match = _GOOGLE_PREVIEW_RE.match(model_id)
        if match:
            return match.group(1)

    # OpenAI aliases
    if model_id in _OPENAI_ALIASES:
        return _OPENAI_ALIASES[model_id]

    # DeepSeek aliases
    if model_id in _DEEPSEEK_ALIASES:
        return _DEEPSEEK_ALIASES[model_id]

    return model_id

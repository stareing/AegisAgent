"""Provider-specific token budget resolution.

Maps adapter types and model names to their context window sizes.
Centralizes what was previously scattered across adapter code.
"""

from __future__ import annotations

# Provider defaults (conservative — real limits may be higher)
PROVIDER_CONTEXT_WINDOWS: dict[str, int] = {
    "anthropic": 200_000,
    "openai": 128_000,
    "google": 1_000_000,
    "deepseek": 128_000,
    "litellm": 128_000,
    "openrouter": 128_000,
    "together": 128_000,
    "groq": 128_000,
    "fireworks": 128_000,
    "mistral": 128_000,
    "perplexity": 128_000,
    "doubao": 128_000,
    "qwen": 128_000,
    "zhipu": 128_000,
    "minimax": 128_000,
    "siliconflow": 128_000,
    "moonshot": 128_000,
    "baichuan": 128_000,
    "yi": 128_000,
    "custom": 8_192,
}

# Model-specific overrides (take precedence over provider defaults)
MODEL_CONTEXT_OVERRIDES: dict[str, int] = {
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-opus-4": 200_000,
    "gpt-4-turbo": 128_000,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "gemini-2.0-flash": 1_000_000,
    "gemini-2.5-pro": 1_000_000,
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
}

CONTEXT_WINDOW_HARD_MIN_TOKENS: int = 16_000
CONTEXT_WINDOW_WARN_BELOW_TOKENS: int = 32_000
DEFAULT_CONTEXT_TOKENS: int = 128_000


def resolve_context_window(
    adapter_type: str,
    model_name: str,
    config_override: int | None = None,
) -> int:
    """Resolve the effective context window for a given adapter and model.

    Priority chain:
    1. Explicit config override (if provided and > 0)
    2. Model-specific override (exact or prefix match)
    3. Provider default
    4. Global default
    """
    if config_override and config_override > 0:
        return config_override

    # Try exact model match first
    if model_name in MODEL_CONTEXT_OVERRIDES:
        return MODEL_CONTEXT_OVERRIDES[model_name]

    # Try prefix match (e.g. "claude-3-opus-20240229" matches "claude-3-opus")
    for prefix, tokens in MODEL_CONTEXT_OVERRIDES.items():
        if model_name.startswith(prefix):
            return tokens

    # Provider default
    return PROVIDER_CONTEXT_WINDOWS.get(adapter_type, DEFAULT_CONTEXT_TOKENS)


def evaluate_context_window_guard(
    tokens: int,
) -> tuple[bool, bool]:
    """Evaluate whether the resolved context window is dangerously small.

    Returns (should_warn, should_block).
    """
    should_block = tokens < CONTEXT_WINDOW_HARD_MIN_TOKENS
    should_warn = tokens < CONTEXT_WINDOW_WARN_BELOW_TOKENS
    return should_warn, should_block

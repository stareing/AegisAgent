"""Model catalog — in-memory registry of available models with metadata.

Provides model discovery, capability querying, and alias resolution.
Pre-populated with known models from provider context windows.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agent_framework.infra.logger import get_logger

logger = get_logger(__name__)


class ModelInfo(BaseModel):
    """Metadata for a single model."""

    model_config = {"frozen": True}

    model_id: str
    name: str = ""
    provider: str = ""
    context_window: int = 0
    supports_vision: bool = False
    supports_audio: bool = False
    supports_parallel_tools: bool = True
    reasoning: bool = False
    aliases: list[str] = Field(default_factory=list)


# Pre-built entries from known models
_KNOWN_MODELS: list[ModelInfo] = [
    ModelInfo(model_id="gpt-4o", name="GPT-4o", provider="openai", context_window=128_000, supports_vision=True),
    ModelInfo(model_id="gpt-4o-mini", name="GPT-4o Mini", provider="openai", context_window=128_000, supports_vision=True),
    ModelInfo(model_id="gpt-4-turbo", name="GPT-4 Turbo", provider="openai", context_window=128_000, supports_vision=True),
    ModelInfo(model_id="gpt-4", name="GPT-4", provider="openai", context_window=8_192),
    ModelInfo(model_id="gpt-3.5-turbo", name="GPT-3.5 Turbo", provider="openai", context_window=16_385),
    ModelInfo(model_id="claude-3-opus", name="Claude 3 Opus", provider="anthropic", context_window=200_000, supports_vision=True),
    ModelInfo(model_id="claude-3-sonnet", name="Claude 3 Sonnet", provider="anthropic", context_window=200_000, supports_vision=True),
    ModelInfo(model_id="claude-3-haiku", name="Claude 3 Haiku", provider="anthropic", context_window=200_000, supports_vision=True),
    ModelInfo(model_id="claude-sonnet-4", name="Claude Sonnet 4", provider="anthropic", context_window=200_000, supports_vision=True),
    ModelInfo(model_id="claude-opus-4", name="Claude Opus 4", provider="anthropic", context_window=200_000, supports_vision=True, reasoning=True),
    ModelInfo(model_id="gemini-2.0-flash", name="Gemini 2.0 Flash", provider="google", context_window=1_000_000, supports_vision=True, supports_audio=True),
    ModelInfo(model_id="gemini-2.5-pro", name="Gemini 2.5 Pro", provider="google", context_window=1_000_000, supports_vision=True, supports_audio=True, reasoning=True),
    ModelInfo(model_id="deepseek-chat", name="DeepSeek Chat", provider="deepseek", context_window=128_000),
    ModelInfo(model_id="deepseek-reasoner", name="DeepSeek Reasoner", provider="deepseek", context_window=128_000, reasoning=True),
]


class ModelCatalog:
    """In-memory model registry with discovery and alias resolution."""

    def __init__(self) -> None:
        self._models: dict[str, ModelInfo] = {}
        self._aliases: dict[str, str] = {}  # alias -> canonical model_id
        # Pre-populate with known models
        for info in _KNOWN_MODELS:
            self.register(info)

    def register(self, info: ModelInfo) -> None:
        """Register a model entry."""
        self._models[info.model_id] = info
        for alias in info.aliases:
            self._aliases[alias.lower()] = info.model_id

    def get(self, model_id: str) -> ModelInfo | None:
        """Look up model by ID or alias."""
        # Direct lookup
        if model_id in self._models:
            return self._models[model_id]
        # Alias lookup
        canonical = self._aliases.get(model_id.lower())
        if canonical:
            return self._models.get(canonical)
        # Prefix match (e.g. "claude-3-opus-20240229" matches "claude-3-opus")
        for mid, info in self._models.items():
            if model_id.startswith(mid):
                return info
        return None

    def resolve(self, model_id: str) -> str:
        """Resolve an alias or prefixed ID to canonical model_id."""
        info = self.get(model_id)
        return info.model_id if info else model_id

    def list_models(self, provider: str | None = None) -> list[ModelInfo]:
        """List all registered models, optionally filtered by provider."""
        models = list(self._models.values())
        if provider:
            models = [m for m in models if m.provider == provider]
        return sorted(models, key=lambda m: m.model_id)

    def list_providers(self) -> list[str]:
        """List all known providers."""
        return sorted({m.provider for m in self._models.values() if m.provider})

    @property
    def count(self) -> int:
        return len(self._models)

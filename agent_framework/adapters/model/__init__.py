from agent_framework.adapters.model.base_adapter import (
    BaseModelAdapter,
    LLMAuthError,
    LLMCallError,
    LLMRateLimitError,
    LLMTimeoutError,
    ModelChunk,
)
from agent_framework.adapters.model.fallback_adapter import FallbackModelAdapter

__all__ = [
    "BaseModelAdapter",
    "FallbackModelAdapter",
    "ModelChunk",
    "LLMCallError",
    "LLMRateLimitError",
    "LLMAuthError",
    "LLMTimeoutError",
]

try:
    from agent_framework.adapters.model.litellm_adapter import LiteLLMAdapter  # noqa: F401

    __all__.append("LiteLLMAdapter")
except ImportError:
    pass

try:
    from agent_framework.adapters.model.openai_adapter import OpenAIAdapter  # noqa: F401

    __all__.append("OpenAIAdapter")
except ImportError:
    pass

try:
    from agent_framework.adapters.model.anthropic_adapter import AnthropicAdapter  # noqa: F401

    __all__.append("AnthropicAdapter")
except ImportError:
    pass

try:
    from agent_framework.adapters.model.google_adapter import GoogleAdapter  # noqa: F401

    __all__.append("GoogleAdapter")
except ImportError:
    pass

try:
    from agent_framework.adapters.model.openai_compatible_adapter import (  # noqa: F401
        OpenAICompatibleAdapter,
        # International
        OpenRouterAdapter,
        TogetherAdapter,
        GroqAdapter,
        FireworksAdapter,
        MistralAdapter,
        PerplexityAdapter,
        # Chinese
        DeepSeekAdapter,
        DoubaoAdapter,
        QwenAdapter,
        ZhipuAdapter,
        MiniMaxAdapter,
        SiliconFlowAdapter,
        MoonshotAdapter,
        BaichuanAdapter,
        YiAdapter,
        # Generic
        CustomAdapter,
    )

    __all__.extend([
        "OpenAICompatibleAdapter",
        "OpenRouterAdapter",
        "TogetherAdapter",
        "GroqAdapter",
        "FireworksAdapter",
        "MistralAdapter",
        "PerplexityAdapter",
        "DeepSeekAdapter",
        "DoubaoAdapter",
        "QwenAdapter",
        "ZhipuAdapter",
        "MiniMaxAdapter",
        "SiliconFlowAdapter",
        "MoonshotAdapter",
        "BaichuanAdapter",
        "YiAdapter",
        "CustomAdapter",
    ])
except ImportError:
    pass

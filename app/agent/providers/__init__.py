from app.agent.providers.anthropic_adapter import AnthropicAdapter
from app.agent.providers.base import ProviderAdapter, ToolCall
from app.agent.providers.openai_adapter import OpenAIAdapter
from app.agent.providers.openrouter_adapter import OpenRouterAdapter

# Every provider the router can select maps to one adapter class here. API keys
# and base URLs are read from the environment inside each adapter's __init__,
# not here - adding a provider means adding a class + one entry, nothing else.
_ADAPTER_CLASSES = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "openrouter": OpenRouterAdapter,
}
_instances: dict[str, ProviderAdapter] = {}


def get_adapter(provider: str) -> ProviderAdapter:
    if provider not in _ADAPTER_CLASSES:
        raise ValueError(f"unknown provider: {provider!r}, expected one of {sorted(_ADAPTER_CLASSES)}")
    if provider not in _instances:
        _instances[provider] = _ADAPTER_CLASSES[provider]()
    return _instances[provider]


__all__ = ["get_adapter", "ProviderAdapter", "ToolCall"]

import os

from app.agent.providers.openai_adapter import OpenAIAdapter


class OpenRouterAdapter(OpenAIAdapter):
    """OpenRouter is OpenAI-wire-compatible - reuse the adapter's request/response
    handling as-is and only point it at a different base URL and key."""

    def __init__(self):
        super().__init__(
            base_url=os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            api_key=os.environ["OPENROUTER_API_KEY"],
        )

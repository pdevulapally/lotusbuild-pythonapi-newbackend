import json

from openai import OpenAI

from app.agent.providers.base import ProviderAdapter, ToolCall
from app.agent.tools import ToolSpec


class OpenAIAdapter(ProviderAdapter):
    supports_images = False
    supports_web_search = False

    def __init__(self, base_url: str | None = None, api_key: str | None = None):
        kwargs = {"timeout": 60.0, "max_retries": 0}
        if base_url:
            kwargs["base_url"] = base_url
        if api_key:
            kwargs["api_key"] = api_key
        self._client = OpenAI(**kwargs)

    def tool_schemas(self, specs: list[ToolSpec], include_web_search: bool = False) -> list[dict]:
        # include_web_search is a no-op here: OpenAI's Chat Completions API has no
        # equivalent opt-in, model-decided web-search tool (verified, not assumed -
        # only fixed *-search-preview models with an always-on web_search_options
        # param exist, which is a different model swap, not a tool to add to this one).
        return [
            {"type": "function", "function": {"name": s.name, "description": s.description, "parameters": s.schema}}
            for s in specs
        ]

    def render(self, messages: list[dict]) -> list[dict]:
        rendered = []
        for msg in messages:
            role = msg["role"]
            if role == "user":
                text = "".join(b["text"] for b in msg["content"] if b["type"] == "text")
                rendered.append({"role": "user", "content": text})
            elif role == "assistant":
                text_parts = [b["text"] for b in msg["content"] if b["type"] == "text"]
                # Anthropic-only server-tool blocks (e.g. web_search) from an earlier
                # turn in this session - fold into a plain text summary rather than
                # silently drop them, since OpenAI/OpenRouter can't render them natively.
                for b in msg["content"]:
                    if b["type"] == "server_tool_use":
                        text_parts.append(f"[used {b['name']} tool]")
                    elif b["type"] == "web_search_tool_result":
                        text_parts.append(f"[web search result: {json.dumps(b['content'])[:2000]}]")
                text = "".join(text_parts)
                tool_use_blocks = [b for b in msg["content"] if b["type"] == "tool_use"]
                entry = {"role": "assistant", "content": text or None}
                if tool_use_blocks:
                    entry["tool_calls"] = [
                        {
                            "id": b["id"],
                            "type": "function",
                            "function": {"name": b["name"], "arguments": json.dumps(b["input"])},
                        }
                        for b in tool_use_blocks
                    ]
                rendered.append(entry)
            elif role == "tool_result":
                # OpenAI has no error flag on tool messages and needs one message per
                # call (not combined) - both are a documented, signaled lossy conversion.
                for block in msg["content"]:
                    text = block["content"] if isinstance(block["content"], str) else json.dumps(block["content"])
                    if block.get("is_error"):
                        text = f"[tool_error] {text}"
                    rendered.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": text})
            else:
                raise ValueError(f"unknown canonical role: {role!r}")
        return rendered

    def create(self, *, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int):
        kwargs = {
            "model": model,
            "messages": [{"role": "system", "content": system}] + self.render(messages),
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = tools
        return self._client.chat.completions.create(**kwargs)

    def is_tool_use(self, response) -> bool:
        return response.choices[0].finish_reason == "tool_calls"

    def text(self, response) -> str:
        return response.choices[0].message.content or ""

    def tool_calls(self, response) -> list[ToolCall]:
        message = response.choices[0].message
        return [ToolCall(tc.id, tc.function.name, json.loads(tc.function.arguments)) for tc in (message.tool_calls or [])]

    def assistant_message(self, response) -> dict:
        message = response.choices[0].message
        blocks = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for tc in (message.tool_calls or []):
            blocks.append(
                {"type": "tool_use", "id": tc.id, "name": tc.function.name, "input": json.loads(tc.function.arguments)}
            )
        return {"role": "assistant", "content": blocks}

    def tool_result_messages(self, results: list[tuple[str, object, bool]]) -> list[dict]:
        content = [
            {"type": "tool_result", "tool_use_id": tool_call_id, "content": result, "is_error": is_error}
            for tool_call_id, result, is_error in results
        ]
        return [{"role": "tool_result", "content": content}]

    def usage(self, response) -> dict:
        return {"input_tokens": response.usage.prompt_tokens, "output_tokens": response.usage.completion_tokens}

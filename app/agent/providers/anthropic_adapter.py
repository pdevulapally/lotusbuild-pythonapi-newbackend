import anthropic

from app.agent.providers.base import ProviderAdapter, ToolCall
from app.agent.tools import ToolSpec


def _plain(value):
    """Recursively convert SDK pydantic objects into plain JSON-safe dicts/lists -
    needed for web-search result blocks, which carry nested SDK types."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, list):
        return [_plain(v) for v in value]
    return value


class AnthropicAdapter(ProviderAdapter):
    supports_images = True
    supports_web_search = True

    def __init__(self):
        self._client = anthropic.Anthropic()

    def tool_schemas(self, specs: list[ToolSpec], include_web_search: bool = False) -> list[dict]:
        schemas = [{"name": s.name, "description": s.description, "input_schema": s.schema} for s in specs]
        if include_web_search:
            schemas.append({"type": "web_search_20260209", "name": "web_search"})
        return schemas

    def render(self, messages: list[dict]) -> list[dict]:
        # Canonical "user"/"assistant" text/tool_use blocks already match Anthropic's
        # own block shape - only the canonical "tool_result" role needs mapping.
        rendered = []
        for msg in messages:
            if msg["role"] != "tool_result":
                rendered.append({"role": msg["role"], "content": msg["content"]})
                continue
            content = []
            for block in msg["content"]:
                result_block = {
                    "type": "tool_result",
                    "tool_use_id": block["tool_use_id"],
                    "content": block["content"],
                }
                if block.get("is_error"):
                    result_block["is_error"] = True
                content.append(result_block)
            rendered.append({"role": "user", "content": content})
        return rendered

    def create(self, *, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int):
        return self._client.messages.create(
            model=model, max_tokens=max_tokens, system=system, tools=tools, messages=self.render(messages)
        )

    def is_tool_use(self, response) -> bool:
        return response.stop_reason == "tool_use"

    def text(self, response) -> str:
        return "".join(b.text for b in response.content if b.type == "text")

    def tool_calls(self, response) -> list[ToolCall]:
        return [ToolCall(b.id, b.name, b.input) for b in response.content if b.type == "tool_use"]

    def assistant_message(self, response) -> dict:
        blocks = []
        for b in response.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
            elif b.type == "server_tool_use":
                # A server-executed tool (e.g. web_search) the model decided to call itself -
                # distinct from "tool_use", never dispatched through run_tool/dispatch_tool_call.
                blocks.append({"type": "server_tool_use", "id": b.id, "name": b.name, "input": _plain(b.input)})
            elif b.type == "web_search_tool_result":
                blocks.append(
                    {"type": "web_search_tool_result", "tool_use_id": b.tool_use_id, "content": _plain(b.content)}
                )
        return {"role": "assistant", "content": blocks}

    def tool_result_messages(self, results: list[tuple[str, object, bool]]) -> list[dict]:
        content = [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": result, "is_error": is_error}
            for tool_use_id, result, is_error in results
        ]
        return [{"role": "tool_result", "content": content}]

    def usage(self, response) -> dict:
        return {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens}

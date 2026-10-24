from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.agent.tools import ToolSpec


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


class ProviderAdapter(ABC):
    """Normalizes one vendor's wire format behind a shared surface so `agent/loop.py`
    never branches on which provider it's talking to.

    `session["messages"]` always holds the CANONICAL, JSON-safe shape (see
    `app/agent/providers/README` in loop.py's docstring) regardless of which
    provider produced a given turn - `render()` is the only place a vendor's
    wire format exists, built fresh from the whole canonical history right
    before each request. This is what makes it safe for pick_model to choose a
    different provider on a later turn than an earlier one in the same session.
    """

    supports_images: bool = False
    supports_web_search: bool = False

    @abstractmethod
    def tool_schemas(self, specs: list[ToolSpec], include_web_search: bool = False) -> list[dict]:
        """Render this vendor's tool definition shape from the shared ToolSpec list.
        `include_web_search` is only meaningful when `supports_web_search` is True."""

    @abstractmethod
    def render(self, messages: list[dict]):
        """Pure: convert the full canonical history into this vendor's wire format."""

    @abstractmethod
    def create(self, *, model: str, system: str, messages: list[dict], tools: list[dict], max_tokens: int):
        """Make one request to the provider. `messages` is canonical - renders it internally."""

    @abstractmethod
    def is_tool_use(self, response) -> bool: ...

    @abstractmethod
    def text(self, response) -> str: ...

    @abstractmethod
    def tool_calls(self, response) -> list[ToolCall]: ...

    @abstractmethod
    def assistant_message(self, response) -> dict:
        """The CANONICAL message to append to session["messages"] for this response."""

    @abstractmethod
    def tool_result_messages(self, results: list[tuple[str, object, bool]]) -> list[dict]:
        """(tool_call_id, content, is_error) tuples -> ONE canonical {"role":"tool_result",...} message."""

    @abstractmethod
    def usage(self, response) -> dict:
        """Normalized {"input_tokens": int, "output_tokens": int}."""

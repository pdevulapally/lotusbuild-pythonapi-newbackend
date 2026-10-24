import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("OPENAI_API_KEY", "test")
os.environ.setdefault("OPENROUTER_API_KEY", "test")

import app.agent.loop as loop_module
from app.agent.loop import _agent_loop
from app.agent.providers.anthropic_adapter import AnthropicAdapter
from app.agent.providers.base import ToolCall
from app.agent.providers.openai_adapter import OpenAIAdapter
from app.agent.tools import ToolSpec
from app.billing.plans import ModelEntry

SCHEMA = {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}
SPECS = [ToolSpec("bash", "Run a shell command.", SCHEMA, handler=lambda s, i: "")]


def test_anthropic_tool_schema_shape():
    schemas = AnthropicAdapter().tool_schemas(SPECS)
    assert schemas == [{"name": "bash", "description": "Run a shell command.", "input_schema": SCHEMA}]


def test_openai_tool_schema_shape():
    schemas = OpenAIAdapter().tool_schemas(SPECS)
    assert schemas == [
        {"type": "function", "function": {"name": "bash", "description": "Run a shell command.", "parameters": SCHEMA}}
    ]


# A canonical history spanning all three roles - the shape pick_model's cross-turn
# provider switch relies on both adapters being able to render identically.
HISTORY = [
    {"role": "user", "content": [{"type": "text", "text": "list files"}]},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Sure, let me check."},
            {"type": "tool_use", "id": "call_1", "name": "bash", "input": {"command": "ls"}},
        ],
    },
    {
        "role": "tool_result",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "a.txt\n", "is_error": False}],
    },
]


def test_anthropic_render_maps_tool_result_to_user_role():
    rendered = AnthropicAdapter().render(HISTORY)
    assert rendered[0] == {"role": "user", "content": [{"type": "text", "text": "list files"}]}
    assert rendered[1]["role"] == "assistant"
    assert rendered[2] == {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "a.txt\n"}],
    }


def test_openai_render_expands_tool_result_into_tool_messages():
    rendered = OpenAIAdapter().render(HISTORY)
    assert rendered[0] == {"role": "user", "content": "list files"}
    assert rendered[1]["role"] == "assistant"
    assert rendered[1]["tool_calls"][0]["function"]["name"] == "bash"
    assert rendered[2] == {"role": "tool", "tool_call_id": "call_1", "content": "a.txt\n"}


def test_openai_render_flags_tool_errors():
    history = [
        {
            "role": "tool_result",
            "content": [{"type": "tool_result", "tool_use_id": "call_2", "content": "boom", "is_error": True}],
        }
    ]
    rendered = OpenAIAdapter().render(history)
    assert rendered == [{"role": "tool", "tool_call_id": "call_2", "content": "[tool_error] boom"}]


def test_anthropic_tool_schema_includes_web_search_when_requested():
    schemas = AnthropicAdapter().tool_schemas(SPECS, include_web_search=True)
    assert {"type": "web_search_20260209", "name": "web_search"} in schemas


def test_openai_tool_schema_ignores_include_web_search():
    # No equivalent opt-in tool exists on OpenAI's Chat Completions API - verified,
    # not assumed - so this is a documented no-op, not a silent gap.
    schemas = OpenAIAdapter().tool_schemas(SPECS, include_web_search=True)
    assert schemas == OpenAIAdapter().tool_schemas(SPECS, include_web_search=False)


def test_openai_render_folds_unknown_anthropic_server_tool_blocks_into_text():
    # A later turn in the same session switched provider after an earlier Anthropic
    # turn used web search - render() must not crash on blocks it can't natively speak.
    history = [
        {
            "role": "assistant",
            "content": [
                {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"query": "weather"}},
                {"type": "web_search_tool_result", "tool_use_id": "srv_1", "content": [{"title": "Weather today"}]},
            ],
        }
    ]
    rendered = OpenAIAdapter().render(history)
    assert rendered[0]["role"] == "assistant"
    assert "web_search" in rendered[0]["content"]
    assert "Weather today" in rendered[0]["content"]


# --- Loop-level: a response with a mix of risky and non-risky tool calls must
# pause the WHOLE batch atomically (can't split one combined tool_result message
# across "ran now" / "ran later") - exercised here via a fake adapter, same
# render-contract reasoning the tests above cover at the adapter level. ---


class _FakeLoopAdapter:
    def __init__(self, calls):
        self._calls = calls

    def create(self, *, model, system, messages, tools, max_tokens):
        return "response"

    def usage(self, response):
        return {"input_tokens": 1, "output_tokens": 1}

    def assistant_message(self, response):
        return {"role": "assistant", "content": []}

    def is_tool_use(self, response):
        return True

    def tool_calls(self, response):
        return self._calls

    def tool_result_messages(self, results):
        return [{"role": "tool_result", "content": results}]


def test_any_risky_call_pauses_the_whole_batch_and_dispatches_nothing():
    calls = [
        ToolCall("call_1", "read_file", {"path": "a.txt"}),
        ToolCall("call_2", "bash", {"command": "rm -rf /x"}),
    ]
    adapter = _FakeLoopAdapter(calls)
    dispatched = []

    loop_module.manager.get = lambda session_id: object()
    loop_module.save_session = lambda session: None
    loop_module.record_usage = lambda uid, choice, usage: None
    loop_module.needs_compaction = lambda messages: False
    loop_module.record_step = lambda *a, **kw: None
    loop_module.dispatch_tool_call = lambda session, sandbox, name, input: dispatched.append(name)

    session = {"id": "s1", "uid": "u1", "messages": [], "system_prompt": "sys", "status": "running"}
    choice = ModelEntry("anthropic", "claude-haiku-5-5", None)

    result = _agent_loop(session, adapter, choice, tools=[])

    assert session["status"] == "awaiting_approval"
    assert dispatched == [], "neither call should have run - a risky call in the batch pauses all of it"
    pending_by_id = {c["id"]: c["risky"] for c in session["pending_approval"]["calls"]}
    assert pending_by_id == {"call_1": False, "call_2": True}
    assert "awaiting approval" in result


if __name__ == "__main__":
    test_anthropic_tool_schema_shape()
    test_openai_tool_schema_shape()
    test_anthropic_render_maps_tool_result_to_user_role()
    test_openai_render_expands_tool_result_into_tool_messages()
    test_openai_render_flags_tool_errors()
    test_anthropic_tool_schema_includes_web_search_when_requested()
    test_openai_tool_schema_ignores_include_web_search()
    test_openai_render_folds_unknown_anthropic_server_tool_blocks_into_text()
    test_any_risky_call_pauses_the_whole_batch_and_dispatches_nothing()
    print("ok")

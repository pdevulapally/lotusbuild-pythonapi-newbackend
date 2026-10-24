import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import app.agent.compaction as compaction_module
from app.agent.compaction import COMPACTION_KEEP_LAST_N, compact, needs_compaction


class FakeAdapter:
    def create(self, *, model, system, messages, tools, max_tokens):
        return {"summary": "did some stuff"}

    def text(self, response):
        return response["summary"]

    def usage(self, response):
        return {"input_tokens": 10, "output_tokens": 5}


compaction_module.get_adapter = lambda provider: FakeAdapter()
compaction_module.record_usage = lambda uid, entry, usage: None


def test_needs_compaction_false_when_short():
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}] * 3
    assert needs_compaction(messages) is False


def test_needs_compaction_true_when_long_and_over_threshold():
    big_text = "x" * 1000
    messages = [{"role": "user", "content": [{"type": "text", "text": big_text}]}] * (COMPACTION_KEEP_LAST_N + 5)
    original = compaction_module.COMPACTION_TOKEN_THRESHOLD
    compaction_module.COMPACTION_TOKEN_THRESHOLD = 100
    try:
        assert needs_compaction(messages) is True
    finally:
        compaction_module.COMPACTION_TOKEN_THRESHOLD = original


def test_compact_keeps_first_and_last_n_and_replaces_middle():
    messages = [{"role": "user", "content": [{"type": "text", "text": f"msg{i}"}]} for i in range(20)]
    session = {"uid": "u1", "messages": list(messages)}
    compact(session)
    result = session["messages"]
    assert result[0] == messages[0]
    assert result[-COMPACTION_KEEP_LAST_N:] == messages[-COMPACTION_KEEP_LAST_N:]
    assert "did some stuff" in result[1]["content"][0]["text"]
    assert len(result) == 2 + COMPACTION_KEEP_LAST_N


if __name__ == "__main__":
    test_needs_compaction_false_when_short()
    test_needs_compaction_true_when_long_and_over_threshold()
    test_compact_keeps_first_and_last_n_and_replaces_middle()
    print("ok")

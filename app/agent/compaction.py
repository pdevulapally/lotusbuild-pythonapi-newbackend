import json
import os

from app.agent.providers import get_adapter
from app.billing.plans import ModelEntry
from app.billing.usage import record_usage

COMPACTION_TOKEN_THRESHOLD = int(os.environ.get("COMPACTION_TOKEN_THRESHOLD", "60000"))
COMPACTION_KEEP_LAST_N = int(os.environ.get("COMPACTION_KEEP_LAST_N", "10"))
COMPACTION_PROVIDER = os.environ.get("COMPACTION_PROVIDER", "anthropic")
COMPACTION_MODEL = os.environ.get("COMPACTION_MODEL", "claude-haiku-5-5")

_SUMMARY_SYSTEM_PROMPT = "Summarize the key facts, decisions, and progress below in under 200 words."


def estimate_tokens(messages: list[dict]) -> int:
    """Cheap chars/4 heuristic - no network call, this runs on every loop iteration."""
    return sum(len(json.dumps(m)) for m in messages) // 4


def needs_compaction(messages: list[dict]) -> bool:
    return len(messages) > COMPACTION_KEEP_LAST_N + 1 and estimate_tokens(messages) > COMPACTION_TOKEN_THRESHOLD


def compact(session: dict) -> None:
    """Replaces everything but the first (original task) message and the last
    COMPACTION_KEEP_LAST_N messages with one summarized text message.

    Keeps session["messages"] within the model's context budget AND within
    Firestore's ~1MiB per-document limit - an uncompacted long session can hit
    the latter regardless of any model's context window.
    """
    messages = session["messages"]
    first = messages[0]
    tail = messages[-COMPACTION_KEEP_LAST_N:]
    middle = messages[1:-COMPACTION_KEEP_LAST_N]
    summary = _summarize(middle, session["uid"])
    session["messages"] = [
        first,
        {"role": "user", "content": [{"type": "text", "text": f"[earlier conversation summarized]\n{summary}"}]},
        *tail,
    ]


def _summarize(middle: list[dict], uid: str) -> str:
    adapter = get_adapter(COMPACTION_PROVIDER)
    response = adapter.create(
        model=COMPACTION_MODEL, system=_SUMMARY_SYSTEM_PROMPT, messages=middle, tools=[], max_tokens=512
    )
    record_usage(uid, ModelEntry(COMPACTION_PROVIDER, COMPACTION_MODEL, quota=None), adapter.usage(response))
    return adapter.text(response)

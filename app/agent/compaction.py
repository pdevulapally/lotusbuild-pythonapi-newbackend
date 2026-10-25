import json
import os

from app.agent.providers import get_adapter
from app.billing.plans import ModelEntry
from app.billing.budgets import metered_create

COMPACTION_TOKEN_THRESHOLD = int(os.environ.get("COMPACTION_TOKEN_THRESHOLD", "8000"))
COMPACTION_KEEP_LAST_N = int(os.environ.get("COMPACTION_KEEP_LAST_N", "10"))

_SUMMARY_SYSTEM_PROMPT = "Summarize the key facts, decisions, and progress below in under 200 words."


def estimate_tokens(messages: list[dict]) -> int:
    """Cheap chars/4 heuristic - no network call, this runs on every loop iteration."""
    return sum(len(json.dumps(m)) for m in messages) // 4


def needs_compaction(messages: list[dict]) -> bool:
    return len(messages) > COMPACTION_KEEP_LAST_N + 1 and estimate_tokens(messages) > COMPACTION_TOKEN_THRESHOLD


def compact(session: dict, choice: ModelEntry) -> None:
    """Replaces everything but the first (original task) message and the last
    COMPACTION_KEEP_LAST_N messages with one summarized text message.

    Uses the turn's authorized model and the same atomic budget guard. This is
    best-effort compaction, not a proof of provider context/document limits;
    oversized retained messages still fail the per-call envelope cap.
    """
    messages = session["messages"]
    first = messages[0]
    tail = messages[-COMPACTION_KEEP_LAST_N:]
    middle = messages[1:-COMPACTION_KEEP_LAST_N]
    summary = _summarize(middle, session["uid"], choice)
    session["_context_compacted"] = True
    session["messages"] = [
        first,
        {"role": "user", "content": [{"type": "text", "text": f"[earlier conversation summarized]\n{summary}"}]},
        *tail,
    ]


def _summarize(middle: list[dict], uid: str, choice: ModelEntry) -> str:
    adapter = get_adapter(choice.provider)
    response = metered_create(
        uid, adapter, choice, system=_SUMMARY_SYSTEM_PROMPT, messages=middle, tools=[], max_tokens=512
    )
    return adapter.text(response)

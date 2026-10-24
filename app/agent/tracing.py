import datetime

from app.firebase import db
from app.store import SESSIONS_COLLECTION
from app.execution.context import run_id


def record_step(
    session_id: str,
    step: int,
    *,
    tool_names: list[str],
    duration_ms: float,
    input_tokens: int,
    output_tokens: int,
    provider: str,
    model: str,
) -> None:
    # A subcollection, not a growing array on the session doc - same Firestore
    # 1MiB-per-document reason app/agent/compaction.py exists.
    db.collection(SESSIONS_COLLECTION).document(session_id).collection("trace").document(f"{run_id() or 'unclaimed'}:{step}").set(
        {
            "step": step,
            "run_id": run_id(),
            "tool": tool_names,
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "provider": provider,
            "model": model,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }
    )


def get_trace(session_id: str) -> list[dict]:
    docs = db.collection(SESSIONS_COLLECTION).document(session_id).collection("trace").stream()
    return sorted((doc.to_dict() for doc in docs), key=lambda t: (t.get("timestamp", ""), t.get("run_id") or "", t["step"]))

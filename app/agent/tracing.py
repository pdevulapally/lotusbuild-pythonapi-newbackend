import datetime

from app.firebase import db


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
    db.collection("sessions").document(session_id).collection("trace").document(str(step)).set(
        {
            "step": step,
            "tool": tool_names,
            "duration_ms": duration_ms,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "provider": provider,
            "model": model,
            "timestamp": datetime.datetime.utcnow().isoformat(),
        }
    )


def get_trace(session_id: str) -> list[dict]:
    docs = db.collection("sessions").document(session_id).collection("trace").stream()
    return sorted((doc.to_dict() for doc in docs), key=lambda t: t["step"])

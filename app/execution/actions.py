"""Bounded receipt shapes and safe projections; RunRepository owns transitions."""
import re

from app.execution.errors import LeaseLost, OutputLimitExceeded
from app.execution.limits import bounded_json, MAX_TOOL_INPUT_BYTES

BATCHES_COLLECTION = "agent_v2_action_batches"
ACTIONS_COLLECTION = "agent_v2_actions"
EVIDENCE_COLLECTION = "agent_v2_action_evidence"
HEX_ID = re.compile(r"[0-9a-f]{64}\Z")
ASSERTIONS = {"effect_observed", "no_effect_observed", "inconclusive"}


def validate_batch(calls):
    if not isinstance(calls, list) or not 1 <= len(calls) <= 32:
        raise OutputLimitExceeded()
    bounded_json(calls, MAX_TOOL_INPUT_BYTES)
    ids = []
    for call in calls:
        if (not isinstance(call, dict) or set(call) != {"id", "name", "input"} or
                not isinstance(call["id"], str) or not 1 <= len(call["id"]) <= 128 or
                not isinstance(call["name"], str) or not isinstance(call["input"], dict)):
            raise LeaseLost("invalid action batch")
        ids.append(call["id"])
    if len(set(ids)) != len(ids):
        raise LeaseLost("duplicate tool call identity")


def projection(item):
    # No raw tool arguments, provider IDs, output, diagnostics or owner identity.
    keys = ("id", "session_id", "batch_id", "sequence", "ordinal", "origin_run_id",
            "name", "effectful", "risky", "input_digest", "state", "version",
            "created_at", "permit", "result_digest", "returned_at", "evidence_sequence")
    value = {key: item[key] for key in keys if key in item}
    if "permit" in value:
        value["permit"] = {key: item["permit"][key] for key in ("run_id", "fence", "issued_at", "lease_expires_at", "run_deadline")}
    return value

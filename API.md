# LotusBuild API: versioned execution foundation

This document describes the review branch's new session contract. It is **not**
compatible with the old Lotus-Build frontend. The redesigned frontend should
use this contract, not the old projects/Redis-event API. Production rollout is
blocked on the unfinished work in [Durable execution](docs/DURABLE_EXECUTION.md).

## Transport and authentication

- Routes have no `/api` prefix. Use HTTPS outside loopback development.
- Protected requests send a Firebase ID token in `Authorization: Bearer …`.
  Ownership mismatches return 404. Do not put tokens in URLs.
- HTTP errors currently use FastAPI's `{"detail": ...}` envelope. Validation
  errors use a detail array; clients must not assume detail is always a string.
- Mutation calls below require a nonblank `Idempotency-Key` of at most 200
  characters. Keep the same key and body for an uncertain-response retry.
- Configure `ALLOWED_ORIGINS` for the actual frontend origin. Credentials/cookies
  are not used by session APIs. Current allowed methods are GET, POST and DELETE.

## Session shape

All session mutation/read routes return HTTP 200 with:

```json
{
  "id": "opaque-id",
  "kind": "code",
  "status": "queued",
  "schema_version": 2,
  "revision": 1,
  "created_at": "2026-10-24T16:00:00+00:00",
  "updated_at": "2026-10-24T16:00:00+00:00",
  "active_run_id": "opaque-job-id",
  "cancel_requested": false,
  "recovery_required": false,
  "error": null,
  "messages": [],
  "plan": null,
  "pending_approval": null
}
```

`revision` is a worker-content version, **not** a complete event cursor; use
`updated_at` and status when comparing snapshots. IDs are opaque. Messages remain
provider-neutral content blocks (`user`, `assistant`, `tool_result`); they are not
yet immutable, paginated transcript records. Compaction can change model context.

### States

| State | Meaning and UI behavior |
|---|---|
| `queued` | Durable job accepted, worker has not begun. Poll; disable another message. |
| `running` | Worker owns the run. Show progress and Cancel. |
| `awaiting_approval` | Show exact pending action details and approve/deny controls. |
| `idle` | Turn succeeded; user may send a follow-up. |
| `cancel_requested` | Cancellation persisted; a remote operation may still be in flight. |
| `cancelled` | No further agent actions are authorized. Cleanup may still be retrying. |
| `error` | Run failed/interrupted; display the bounded error. |

If `recovery_required` is true, do not offer automatic retry/resume. External
action outcomes may be uncertain. Reconciliation and a new session are needed.
A cancelled or error session cannot receive new messages in this foundation.

## Core routes

### `POST /sessions`

Body: `{"task":"user goal","kind":"code"}`. `kind` is `code` or `desktop`;
`task` is 1–32,000 characters. Unknown fields are rejected.
Requires an idempotency key. Returns `queued`. No provider is called by HTTP.

### `GET /sessions/{id}`

Returns the current session snapshot. Use this after replayed mutations: an
idempotent receipt returns the original response, which may now be stale.

### `POST /sessions/{id}/messages`

Body: `{"content":"follow-up"}`, 1–32,000 characters. Requires an idempotency key.
Only an idle session accepts another turn. Returns queued; an active/cancelled/
interrupted session returns 409. Unknown fields are rejected.

### `POST /sessions/{id}/approve`

```json
{
  "approval_id": "pending_approval.id",
  "decisions": [{"call_id":"call-1","approved":true}]
}
```

Requires an idempotency key. `approval_id` must match the current pending batch.
Supply exactly one decision for each call with `risky: true`; omit non-risky IDs.
Duplicates, missing or extra IDs are rejected. A stale batch returns 409.
The whole tool batch resumes in one durable job, with denied risky calls recorded
as denials. Different idempotency keys cannot claim the same batch twice.

`pending_approval` contains:

```json
{
  "id": "opaque-batch-id",
  "provider": "provider-name",
  "model": "model-id",
  "quota": null,
  "steps_used": 0,
  "calls": [{"id":"call-1","name":"bash","input":{"command":"..."},"risky":true}]
}
```

Render the action and its exact arguments before asking for approval. A generic
“build this plan” message is not an approval. Regex risk classification remains
insufficient as a production security boundary; the worker is experimental.

### `DELETE /sessions/{id}`

No body or key. Repeated cancellation is safe. Returns the session with
`cancel_requested` or `cancelled`, not the old `{"status":"closed"}` object.
This requests cancellation and sandbox cleanup; it does not delete history and
does not undo already completed external effects. New access to preview/desktop/
screenshot is blocked after cancellation. Existing external URLs remain subject
to provider termination and must not be treated as immediately revoked.

### `GET /sessions/{id}/stream`

Authenticated fetch-based SSE, not native browser EventSource. Sends id-less JSON
session snapshots when updated. Closes on idle/error/cancelled/awaiting_approval.
There are no Redis event IDs, token deltas or `Last-Event-ID` replay semantics yet.
The first frontend slice should poll GET snapshots or explicitly handle snapshot
stream reconnects by fetching canonical state. On error, stop and re-read state.

## Sandbox, trace and memory routes

- `GET /sessions/{id}/preview?port=3000` → `{"url":"https://…"}`.
- `GET /sessions/{id}/desktop` → `{"stream_url":"https://…"}`; not a noVNC ticket.
- `GET /sessions/{id}/screenshot` → PNG bytes.
- `GET /sessions/{id}/trace` → trace entries with `run_id`, `step`, tool list,
  duration, token counts, provider/model and timestamp, ordered chronologically.
- `GET /memory` → `{"notes":[…]}`; `DELETE /memory` → `{"status":"cleared"}`.

Sandbox access requires an existing live sandbox. Saved workspace files do not
currently survive provider expiration: snapshot/restore is not implemented.

## Other existing surfaces

`GET /health` is process liveness only, not dependency readiness. Billing routes
remain `/billing/checkout`, `/billing/portal`, `/billing/webhook`. Checkout uses
`{"tier_id":"…"}` and requires a key. Billing/connector-specific authorization
and rollout are separate from the new session workflow; do not enable them just
because the task UI works. GitHub write tools are intentionally disabled.

## Frontend acceptance gates

1. Start task, preserve key during uncertain response, render queued/running.
2. Reopen from a known session ID and reconcile state after network interruption.
3. Render pending action arguments, approve/deny the exact batch, handle 409.
4. Cancel while queued/running/awaiting approval; never show cancellation as undo.
5. Render error/recovery-required without automatically repeating side effects.
6. Keep Firebase identity separate from session IDs; clear data when user changes.

There is not yet a session-list endpoint, resumable event log, projects API,
attachments API or full account bootstrap contract in this branch.

# LotusBuild API: versioned execution foundation

## Local native scoped task authorization

Explicit consent, inspect/revoke and grant-scoped queue/dispatch routes now have
source and synthetic/local emulator tests. They reuse existing auth, Plan, run
fences, receipts and BudgetLedger. No actual access grants or default automatic
drain are enabled. Required policy values and a bounded account reader are absent
by default; missing configuration fails503. See
[`docs/TASK_AUTHORIZATION_CONTRACT.md`](docs/TASK_AUTHORIZATION_CONTRACT.md)
for exact route/field/version/expiry/resource and production integration gates.
Shared request verification rejects nondefault Firebase tenant namespaces.

## Optional native selection and Plan mode

`POST /sessions` accepts optional `model` (an exact ID in the current user's
configured plan) and `mode` (`build`/`plan`). Explicit models never fall back.
`POST /sessions/{id}/messages` accepts mode for an idle next turn; model/effort
cannot change in messages. Snapshots expose model/effort/mode while retaining
native approval, cancellation and recovery states. Omitted selections retain
native behavior and historical create idempotency fingerprints.

Explicit efforts low/standard/high/xhigh currently return 503
`session/model-unavailable`: native plans lack verified capability metadata and
paid effort ceilings. Invalid names, nulls, types and extra metadata reject via
the shared 422 envelope. No accepted effort request silently uses a default.

Plan grants only the implemented verified read/list/search subset: read_file.
The parent checks entire batches and every action permit, including approval
resume. Shell commands, aliases, GUI and virtual mutation tools cannot bypass
the policy. See `docs/MODEL_PLAN_CONTRACT.md` for implementation evidence and
`docs/LOCAL_CONFIGURATION_CHECKLIST.md` for variable names and remaining gaps.

This document describes the native versioned execution session contract. Current
Lotus-Build frontend `8173d7c` still uses projects and its existing session
schemas; it is not fully compatible with this API. The `/events` transport is
now aligned, while project/model/mode/lifecycle decisions remain open. See
[`docs/SESSION_EVENTS_CONTRACT.md`](docs/SESSION_EVENTS_CONTRACT.md). Production rollout is
blocked on the unfinished work in [Durable execution](docs/DURABLE_EXECUTION.md).

## Transport and authentication

- Routes have no `/api` prefix. Use HTTPS outside loopback development.
- Protected requests send a Firebase ID token in `Authorization: Bearer …`.
  Ownership mismatches return 404. Do not put tokens in URLs.
- HTTP errors retain `detail` and also include the current frontend envelope
  `{"error":{"code":"...","message":"..."}}`. Validation detail is an array
  of location/type/message fields without submitted input. Clients must not
  assume detail is always a string. Expired authentication returns the specific
  `auth/token-expired` code, enabling the client's one refresh/retry.
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
Both snapshot and event streams share bounded admission/I/O and token/owner
reauthorization. Expiry is checked before each emission; revocation/ownership
uses at most a one-second authorization lease. Snapshot changes compare the full
public payload. Authorization failures close the stream with a sanitized error.

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

There is not yet a session-list endpoint, projects API, attachments API or full
account bootstrap contract in this branch.

`GET /sessions/{id}/events` provides durable, bounded lifecycle replay
with `Last-Event-ID`. Native states remain explicit; this does not add projects,
immutable transcript records, model/effort selection, Plan enforcement, saved
files or the complete current frontend session response. The existing `/stream`
snapshot API is retained separately for native clients.

## Experimental resource admission (review batch)

Session create/message/approval submissions can return **429** when per-user
active-run or reserved-sandbox capacity is exhausted. Defaults (2 runs / 3
sandboxes) are configurable development limits, not subscription-tier promises.
Idle and approval-waiting sessions retain sandbox capacity until cancelled and
cleanup is confirmed; uncertain failures require operator reconciliation.

Model calls now reserve daily requests/monthly tokens before each provider
invocation, including compaction and approval resumes. They require the current
entitlement, permit at most 4,096 output tokens and a bounded text envelope,
and fail closed on unsupported multimedia inputs. Native provider web search is
disabled pending separately verified metering. Desktop routes remain, but this
batch does not provide metered autonomous screenshot reasoning. See
[`docs/EXECUTION_BUDGETS.md`](docs/EXECUTION_BUDGETS.md) for precise guarantees,
feature reductions, migration blockers, and development-only limitations.

## Experimental execution stopping

Shared worker boundaries enforce run deadlines/cancellation and bound tool
arguments, results and durable session data. Oversized submissions return 413
with `request/payload-too-large`. A tool exception after possible effects stops
the run and retains recovery-required state/capacity; no automatic replay occurs.
`SessionOut.recovery` optionally reports a fixed `reason` and server-owned `tool`
name. This is manual recovery metadata, not a safe-retry instruction or a durable
external-action receipt. Historical recovery states may lack this metadata.

Initial stopping limits and tested boundaries are in
[`docs/EXECUTION_SAFETY.md`](docs/EXECUTION_SAFETY.md). The worker remains guarded.
The subsequent [process supervision slice](docs/PROCESS_SUPERVISION.md) adds
owned Windows execution/cleanup processes. Terminal idle/approval publication
waits for validated completion and local process exit. Forced local termination
retains recovery-required state; it does not prove remote effects stopped.


## Experimental durable action receipts

Authenticated owners can inspect paginated `/sessions/{id}/actions` and
`/sessions/{id}/actions/{action_id}`. POST
`/sessions/{id}/actions/{action_id}/reconciliation-evidence` requires the ordinary
mutation rate limit, `Idempotency-Key`, strict expected version, fixed assertion
and SHA256 evidence digest. It creates an immutable **unverified owner
attestation** only. It cannot mark success, retry, refund, clear recovery or
release slots. No evidence URLs or raw tool arguments/results are accepted.

Stable parent-owned intent/permit/result records now distinguish prepared work,
unknown outcomes and handler returns across approval and crash windows. Neither
handler returns nor owner assertions verify external success. See
[`docs/ACTION_RECEIPTS.md`](docs/ACTION_RECEIPTS.md) for schemas, testing,
no-replay rules and remaining trusted reconciliation dependencies.


## Project and durable text-history checkpoint (local guarded slice)

See [ACCOUNT_PROJECT_HISTORY_CONTRACT.md](docs/ACCOUNT_PROJECT_HISTORY_CONTRACT.md)
for exact source revision, route/schema/pagination rules, configuration blockers
and native-vs-current-client compatibility. Project create/list/read/name-description
PATCH and original text GET messages are implemented. New project session create/
list and POST cancel require explicit `native=true` for native SessionOut; default
current-client projection503s before mutation. Existing native session endpoints
retain their contracts. `/me` and `/me/usage`503; queues, generic session updates,
project deletion and saved-content routes501 after ownership checks. There is no
full frontend parity or live verification claim. Project execution additionally
requires verified typed ai.run/platform.web/ai.model.allowlist in the current plan;
missing configuration fails closed and cannot grant a missing native model. The
first project workspace binding remains exclusive until a verified adoption/release
contract exists. No destructive delete or migration was added.


## Finite native next-turn queue

See [DURABLE_QUEUE_CONTRACT.md](docs/DURABLE_QUEUE_CONTRACT.md) for actual native
enqueue/list/inspect/cancel/fresh-owner dispatch routes, required explicit expiry,
verified build-meter policy, receipts/FIFO/approval fences and retained workspace.
Automatic worker drain defaults closed without a trusted fresh-authority reader;
no standing durable task grant or scheduler is created. Historical queue501 notes
above are superseded for these native routes only. Existing frontend prompt-based
message POST/steering, lifecycle UI and account/catalog compatibility stay blocked.

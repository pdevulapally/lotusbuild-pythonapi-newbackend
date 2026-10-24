# Lotus Build Agent API

An autonomous coding/desktop agent backend (Devin/Claude-Code/ChatGPT-agent style). This document is the integration reference for a frontend/client talking to it over HTTP. It reflects the code exactly — every field, status code, and header below is real, not aspirational.

## Base concepts

- **Auth**: every endpoint except `GET /health`, `POST /billing/webhook`, and `GET /auth/github/callback` requires `Authorization: Bearer <firebase-id-token>` (a Firebase Auth ID token for the signed-in user). A missing/invalid/revoked token returns `401`.
- **Tenant isolation**: every session is scoped to the uid that created it. Accessing another user's session returns `404` (not `403` — existence isn't leaked either).
- **Idempotency**: every mutating endpoint that has a real side effect (`POST /sessions`, `POST /sessions/{id}/messages`, `POST /sessions/{id}/approve`, `POST /billing/checkout`) **requires** an `Idempotency-Key` header. Missing it → `422` (FastAPI's own required-header validation). See [Idempotency](#idempotency) below.
- **Async execution**: `POST /sessions` and `POST /sessions/{id}/messages` return **immediately** — the agent runs in the background. Poll `GET /sessions/{id}` or open `GET /sessions/{id}/stream` to watch it work. This is deliberate: it's what makes the API usable over a flaky connection (see [Background execution](#background-execution--connectivity)).
- **Rate limiting**: a per-IP limit applies to every request, and a tighter per-user limit applies to the mutating endpoints above. Over the limit → `429 {"detail": "rate limit exceeded for '<bucket>'"}`.
- **Errors**: `HTTPException`-raised errors (`400`/`401`/`404`/`409`/`422`/`429`) return `{"detail": "<message>"}` with a real, specific message. Any genuinely unhandled server error returns a sanitized `500 {"detail": "internal server error"}` — never a stack trace.

---

## Session lifecycle

A **session** is one agent run in one E2B sandbox (`"code"` = headless Linux, `"desktop"` = full GUI Linux desktop you can screenshot/click). Its JSON shape (`SessionOut`, returned by every session endpoint below) is:

```json
{
  "id": "a1b2c3...",
  "kind": "code",
  "status": "running",
  "error": null,
  "messages": [ ... ],
  "plan": null,
  "pending_approval": null
}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Session id, used in every other `/sessions/{id}/...` call. |
| `kind` | `"code"` \| `"desktop"` | Which sandbox backend this session runs in. |
| `status` | `"running"` \| `"idle"` \| `"awaiting_approval"` \| `"error"` | See [Status values](#status-values). |
| `error` | string \| null | Set only when `status == "error"` — what went wrong. |
| `messages` | array of canonical message objects | The full conversation so far, in the shape below. This is what you render as the transcript. |
| `plan` | array \| null | The agent's own visible task list, if it's called `update_plan`. Shape: `[{"step": "...", "status": "pending"|"in_progress"|"done"}, ...]`. |
| `pending_approval` | object \| null | Present only when `status == "awaiting_approval"`. See [Approval flow](#approval-flow). |

### Status values

| Status | Meaning | What the client should do |
|---|---|---|
| `running` | The agent is actively working (in a background task). | Poll or stream; don't send another message yet (`409` if you do). |
| `idle` | The agent finished its turn and is waiting for you. | Safe to send the next message. |
| `awaiting_approval` | The agent wants to run a risky action and is paused for a human decision. | Show `pending_approval` to the user, then call `POST /sessions/{id}/approve`. |
| `error` | The background task failed. | Read `error` for the message. The session is otherwise still readable; you can still send a new message to try to continue. |

### Message shape

Every entry in `messages` is one of four canonical shapes (provider-agnostic — the backend may switch between Anthropic/OpenAI/OpenRouter between turns depending on your plan's quota, but the shape your client sees never changes):

```jsonc
// from the user
{"role": "user", "content": [{"type": "text", "text": "..."}]}

// from the agent, no tool calls
{"role": "assistant", "content": [{"type": "text", "text": "..."}]}

// from the agent, with one or more tool calls (text block is optional)
{"role": "assistant", "content": [
  {"type": "text", "text": "Let me check that."},
  {"type": "tool_use", "id": "call_1", "name": "bash", "input": {"command": "ls"}}
]}

// the result of running those tool call(s)
{"role": "tool_result", "content": [
  {"type": "tool_result", "tool_use_id": "call_1", "content": "a.txt\n", "is_error": false}
]}
```

A `tool_use` block's `content` for a `screenshot` call is a list of image blocks (`[{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "..."}}]`) rather than a string — render it as an image, not text, when `tool_use_id` corresponds to a `screenshot` call. If the agent used Anthropic's web search tool, you may also see `{"type": "server_tool_use", ...}` / `{"type": "web_search_tool_result", ...}` blocks — treat unknown block types as opaque/collapsible detail, not an error.

---

## Endpoints

### `POST /sessions` — start a new session

**Headers**: `Authorization` (required), `Idempotency-Key` (required).
**Body**:
```json
{"task": "Set up a FastAPI hello-world app and get it running", "kind": "code"}
```
`kind` defaults to `"code"`; use `"desktop"` for a GUI sandbox (screenshot/click/etc. tools).

**Response** `200`: a `SessionOut` with `status: "running"` — **the agent has not finished yet**, it's just been scheduled. Poll or stream to see it progress.

**Errors**: `400` (unknown `kind`), `409` → impossible here (new session), `429` (rate-limited quota exhausted is NOT raised here anymore — see note below).

> **Behavioral note**: if the user's plan has no model quota left at all, that failure happens inside the background task, not synchronously on this call. You will get a `200` with `status: "running"`, and the *next* poll will show `status: "error"` with `error` describing the quota exhaustion. Always check `status` after the fact — a `200` here only means "accepted," not "succeeded."

### `GET /sessions/{id}` — read current state

**Headers**: `Authorization`.
**Response** `200`: current `SessionOut`. This always reads live state — safe to poll repeatedly, and safe across multiple backend instances (there's no "sticky" instance).
**Errors**: `404` if the session doesn't exist or isn't yours.

### `GET /sessions/{id}/stream` — live updates via SSE

**Headers**: `Authorization`.
**Response**: `Content-Type: text/event-stream`. One `data: <SessionOut JSON>\n\n` event each time the session changes (new message appended, or a status change), roughly once per second while something is happening. The stream closes itself once `status` becomes `idle` or `error`.

> ⚠️ **Browser caveat**: the native `EventSource` API cannot send custom headers, so it **cannot** carry your `Authorization` bearer token. `new EventSource(url)` from a browser will get a `401`/`422`. Use a `fetch()`-based SSE reader (e.g. the `fetch` + `ReadableStream`/`TextDecoder` pattern, or a library like `@microsoft/fetch-event-source`) that can attach the header, or fall back to polling `GET /sessions/{id}` on an interval from the browser. Do not work around this by passing the token as a `?token=` query parameter — that leaks it into server/proxy access logs.

### `POST /sessions/{id}/messages` — continue a session

**Headers**: `Authorization` (required), `Idempotency-Key` (required).
**Body**: `{"content": "now add a /health endpoint"}`
**Response** `200`: `SessionOut` with `status: "running"` — again, scheduled, not finished. Poll/stream for the result.
**Errors**:
- `404` — session not found/not yours.
- `409 {"detail": "session is running"}` or `409 {"detail": "session is awaiting_approval"}` — you can't send a new message while the agent is mid-turn or paused for approval. Wait for `idle` (or resolve the approval first).
- `429` — per-user rate limit.

### `POST /sessions/{id}/approve` — resolve a pending approval

Only valid when `status == "awaiting_approval"`. See [Approval flow](#approval-flow) for the full shape of `pending_approval` and worked example.

**Headers**: `Authorization` (required), `Idempotency-Key` (required).
**Body**:
```json
{"decisions": [{"call_id": "call_2", "approved": true}]}
```
You only need to include a decision for calls flagged `"risky": true` in `pending_approval.calls` — non-risky calls in the same paused batch run automatically regardless of what you send.

**Response** `200`: `SessionOut` with `status: "running"` — the agent resumes in the background from exactly where it paused.
**Errors**:
- `409 {"detail": "no pending approval on this session"}` — nothing to approve (`status` isn't `awaiting_approval`).
- `400 {"detail": "must decide on every risky pending call"}` — you omitted a decision for a call flagged `risky: true`.

### `DELETE /sessions/{id}` — end a session

Kills the E2B sandbox and removes its registry entry. **No `Idempotency-Key` needed** — deleting an already-gone session is a no-op, not an error.

**Response** `200`: `{"status": "closed"}`.

### `GET /sessions/{id}/preview?port=3000` — public preview URL

For a `"code"` session where the agent started a server inside the sandbox (e.g. `uvicorn ... --port 3000`). Returns `{"url": "https://3000-<sandbox-id>.e2b.app"}` — share this with whoever needs to see the running app. Treat it like a credential: anyone with the URL can reach that port.

**Errors**: `400` if the sandbox is gone/expired (clear message, not a crash).

### `GET /sessions/{id}/desktop` — live VNC stream URL (desktop sessions only)

Returns `{"stream_url": "..."}` — an embeddable URL showing the live desktop. Starting the stream is idempotent (safe to call repeatedly; it only actually starts once).

**Errors**: `400` if this isn't a desktop-kind session, or the sandbox is gone.

### `GET /sessions/{id}/screenshot` — one-off screenshot (desktop sessions only)

Returns raw `image/png` bytes directly (not JSON) — useful for a thumbnail/preview without opening the full VNC stream.

**Errors**: `400` if this isn't a desktop-kind session.

### `GET /sessions/{id}/trace` — per-step execution trace

Returns an array, one entry per loop iteration the agent took:

```json
[
  {
    "step": 0, "tool": ["bash"], "duration_ms": 842.1,
    "input_tokens": 1204, "output_tokens": 312,
    "provider": "anthropic", "model": "claude-sonnet-5-5",
    "timestamp": "2026-10-24T18:03:11.482910"
  }
]
```
`tool` is a list because one step can make several tool calls at once; it's `[]` for a step that produced only a text reply. Useful for a "show me what it actually did and how long each step took" debug/cost view.

### `GET /memory` / `DELETE /memory` — per-user agent memory

The agent calls a `remember` tool to save durable facts about you across sessions (preferences, project conventions — not task-specific details). `GET /memory` → `{"notes": ["likes dark mode", "uses pnpm not npm", ...]}`. `DELETE /memory` clears it (`{"status": "cleared"}`). Neither needs an `Idempotency-Key` (both naturally idempotent).

---

## Billing

### `POST /billing/checkout`

**Headers**: `Authorization`, `Idempotency-Key`.
**Body**: `{"tier_id": "pro"}` (one of the configured plan tiers — `free`/`pro`/`bloom`/`teams`/`business`, or whatever's seeded).
**Response** `200`: `{"url": "https://checkout.stripe.com/..."}` — redirect the user here to pay.
**Errors**: `400` if the tier doesn't exist or has no price configured yet.

### `POST /billing/portal`

**Headers**: `Authorization`. No idempotency key (safe to call repeatedly — it's just a read + a Stripe portal-session create, not state-changing on our side).
**Response** `200`: `{"url": "https://billing.stripe.com/..."}` — redirect the user here to manage/cancel their subscription.
**Errors**: `400` if the user has no billing customer yet (hasn't ever checked out).

### `POST /billing/webhook`

**Not for the frontend** — this is Stripe's own server calling us, authenticated by Stripe-Signature header verification, not a bearer token. Mentioned here only so you know it exists and don't need to implement subscription-status polling yourself: once a checkout completes, the user's plan tier updates automatically via this webhook, and `pick_model`'s behavior (which models/quota a session gets) reflects it on the very next session.

---

## GitHub integration

### `GET /auth/github/start`

**Headers**: `Authorization`. This is a **redirect** endpoint, not a JSON API — point the user's browser at it directly (e.g. `window.location = apiBase + "/auth/github/start"` with the `Authorization` header... see note below) and it 302s them to GitHub's consent screen.

> Since redirecting the browser can't carry a bearer header either, the practical integration is: your frontend calls this endpoint itself (server-side, or via `fetch` with the header, `redirect: "manual"`) to get the `Location` URL, then navigates the browser there — rather than linking to it directly from markup.

### `GET /auth/github/callback?code=...&state=...`

GitHub redirects here after consent. **No auth header involved** — the `state` parameter (which this backend generated and signed) is what proves which user this is for. Not something your frontend calls directly; just make sure `GITHUB_REDIRECT_URI` in the backend's config matches a route your app doesn't intercept. On success it redirects to `GITHUB_OAUTH_SUCCESS_REDIRECT_URL` (configured server-side) — that's the page your frontend should treat as "GitHub linked."

Once linked, the agent can use its `create_pull_request` tool in any session for that user (this is a risky/approval-gated tool — see below).

---

## Approval flow

Some tool calls are **risky** by policy (always: `expose_port`, `create_pull_request`; conditionally: a `bash` command matching a destructive pattern like `rm -rf`, `sudo`, a force-push, etc.) and pause the agent for a human decision rather than running automatically.

**Important atomicity rule**: if a single agent turn tries to call multiple tools at once and *any* of them is risky, the **entire batch** pauses — including the non-risky ones. They all auto-run together once you resume; you only ever need to supply a decision for the ones flagged `risky: true`.

When this happens, `GET /sessions/{id}` (or the stream) shows:

```json
{
  "status": "awaiting_approval",
  "pending_approval": {
    "provider": "anthropic",
    "model": "claude-sonnet-5-5",
    "quota": null,
    "steps_used": 3,
    "calls": [
      {"id": "call_5", "name": "read_file", "input": {"path": "README.md"}, "risky": false},
      {"id": "call_6", "name": "bash", "input": {"command": "rm -rf build/"}, "risky": true}
    ]
  },
  "...": "..."
}
```

Render this as: "The agent wants to run `rm -rf build/` — approve?" (the non-risky `read_file` doesn't need a UI prompt; it'll just run). Then:

```http
POST /sessions/{id}/approve
Idempotency-Key: <a fresh uuid>

{"decisions": [{"call_id": "call_6", "approved": true}]}
```

A rejected call isn't silently dropped — the agent sees a tool result saying `"user denied this action"` and can react to it in its next step (explain alternatives, try something else, ask a follow-up), rather than the conversation just stalling.

---

## Idempotency

`Idempotency-Key` is a client-generated opaque string (a UUID is fine) that you should generate **once per logical action** and reuse on retry — never generate a new one when retrying the *same* request after a timeout/dropped connection.

- First call with a given key: runs normally.
- A retry with the **same** key after the first call already succeeded: returns the **exact same response** without re-running anything (no duplicate sandbox, no duplicate charge, no duplicate agent turn).
- A retry while the first call is **still in flight**: `409 {"detail": "request with key '...' is already being processed"}` — back off and poll `GET /sessions/{id}` instead of hammering retries.
- If the first call's handler actually failed (raised an error), the key is freed — a retry with the same key runs fresh.

This is what makes the API safe to use from a flaky connection (a train, a tunnel, a dropped wifi handoff): if you're not sure whether your `POST /sessions/{id}/messages` actually landed, just retry it with the same `Idempotency-Key` — worst case you get back the state from the attempt that did land.

## Background execution & connectivity

`POST /sessions` and `POST /sessions/{id}/messages` return almost immediately (a Firestore write, not a multi-minute agent turn) specifically so a flaky connection only has to survive a short request. The actual agent work happens server-side regardless of whether your connection stays up. The integration pattern for any client, especially on uncertain connectivity:

1. `POST` with a fresh `Idempotency-Key`. If the request itself fails/times out, retry with the **same** key.
2. As soon as you get a `200` back (`status: "running"`), start polling `GET /sessions/{id}` (every 1–2s) or open `GET /sessions/{id}/stream`.
3. If the connection drops entirely, it doesn't matter — reconnect whenever and resume polling/streaming `GET /sessions/{id}`. The work already in progress is untouched; you just missed some intermediate updates, not the final state.
4. When `status` becomes `idle`, read the new `messages`. If it becomes `awaiting_approval`, prompt and call `/approve`. If it becomes `error`, show `error` and let the user decide whether to retry (`POST /sessions/{id}/messages` with a new message, or just start a new session).

## Reference: tool names you may see in `messages`/`pending_approval`

| Tool | What it does | Sandbox kind | Risky? |
|---|---|---|---|
| `bash` | Run a shell command | any | conditionally (destructive patterns) |
| `read_file` / `write_file` | Read/write a file | any | no |
| `expose_port` | Get a public preview URL for a port | any | **always** |
| `git_commit` / `git_diff` | Local git operations | any | no |
| `create_pull_request` | Push + open a real GitHub PR | any (needs GitHub linked) | **always** |
| `screenshot` / `click` / `move_mouse` / `type_text` / `press_key` / `scroll` | GUI control | `desktop` only | no |
| `update_plan` | Set the visible task list (→ `SessionOut.plan`) | any | no |
| `remember` | Save a durable note about the user | any | no |
| `web_search` | Anthropic-only hosted web search | any, Anthropic turns only | no |

This list can grow; treat an unrecognized tool name the same as any other — render its name/input/result generically rather than erroring.

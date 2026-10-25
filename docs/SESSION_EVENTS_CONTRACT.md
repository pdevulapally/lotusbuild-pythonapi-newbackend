# Current frontend contract alignment: durable lifecycle events

Subsequent bounded selection/policy slice: `MODEL_PLAN_CONTRACT.md` documents
exact configured native-model requests, persisted mode changes and enforced Plan
capabilities. Explicit effort/catalog parity remains blocked by missing verified
capability and paid-ceiling configuration. Historical findings below describe
the event checkpoint; native lifecycle and other unresolved contracts remain.

## Scope and actual source

This slice aligns the event transport of the current frontend `master`
`8173d7c27f6bbb3c9fe040782d1a891219cffc45` with the existing durable FastAPI run
engine. Current TypeScript server `89a1bcac115e26a07c8990a3f491e84ee278b2ff` was
also inspected. The frontend and Desktop original checkouts were not changed.
This is a complete local event-journal slice, not complete project/session UI
compatibility or a working replacement deployment.

## Authoritative write and replay path

- `RunRepository._write_session` atomically commits each session change and an
  immutable `agent_v2_events` record. API acceptance, worker starts/updates,
  approval pauses, cancellation and recovery use this one boundary. No separate
  runner, event publisher or second state-transition implementation was added.
- Event counters/timestamps derive from transaction-read state. Worker saves
  reject changed session identity/ownership and cannot reset journal metadata.
  Create-only writes prevent silent replacement of an existing event.
- IDs are `milliseconds-sequence`, matching the frontend's string cursor format.
  Per-session sequence is strict; timestamps do not move backward with the clock.
  Idempotency replay does not append another event.
- `event_history` checks session ownership before inspecting a supplied cursor,
  returns at most 100 records through an observed session head, and requires
  contiguous sequences. Missing/corrupt history fails closed rather than silently
  advancing a client past a gap. A cursor must exist in this session's journal.
- Events contain only session ID, native status, content revision and a boolean
  recovery flag. No transcript, prompt, tool input/result, source, model output,
  connector credentials, exception text or owner identity is published.

The declared replay index is in `firestore.indexes.json`; it was not deployed.
New events are not retroactive history for pre-upgrade sessions. There is no
automatic event TTL or guessed retention policy. Deletion/export/retention work
must include this collection before production adoption.

## HTTP and streaming behavior

`GET /sessions/{id}/events` requires the ordinary Firebase bearer header.
Optional `Last-Event-ID` resumes after a previously applied event. No query-token
authentication exists. Session ownership and cursor validity are checked before
successful stream headers. CORS now explicitly permits `Last-Event-ID`; origins,
credentials and allowed HTTP methods were not broadened.

Frames have `id`, `event` and JSON `data`; `stream.closing` control frames have no
ID. The journal records `session.queued`, `session.running`, `session.updated`,
`session.idle`, `session.failed`, `session.awaiting_approval`,
`session.cancel_requested` and `session.cancelled`. Stopping or waiting for
approval is never translated into successful completion.

Both event and snapshot streams now reuse the shared token verifier behind
`get_uid`, including Firebase revocation verification. A shared gate checks
expiry before each emission and refreshes revocation/ownership within an
authorization lease of one second, anchored before verification/ownership I/O.
Slow checks fail closed. Expired credentials end with `token-expired`; other
authorization loss ends with `authorization-failed`; storage/cursor failures
end with `history-unavailable`. An idle/terminal session remains observable for
later turns until disconnect or authorization failure. Streams send keepalive
comments while waiting and fetch another page rather than buffer unbounded history.

There are development process-local limits of 100 streams and 2 per UID. These
are socket bounds, separate from paid run/sandbox admission. ASGI sends, database
reads and reauthentication waits have a default 10-second operation timeout,
configured by `SSE_OPERATION_TIMEOUT_SECONDS` (positive, at most 3600).
`SSE_POLL_INTERVAL_SECONDS` remains the polling setting. These are development
defaults, not tested production capacity or an SLA.

A stalled send ends the response and releases its slot; a closing frame cannot
be guaranteed to reach an already blocked reader. Every ASGI completion/error/
disconnect releases the stream slot. Cancelling an asyncio wait does not kill
an arbitrary SDK thread. Real proxy/browser backpressure and deployment timeout
behavior remain unverified.

The shared error formatter retains native `detail` and adds the current client
`error.code/message` envelope. It is reused for HTTP, validation, command,
idempotency, rate-limit and generic errors. Initial expired-token errors expose
`auth/token-expired`; token verification itself remains in the shared auth
dependency. This enables the client's existing one-time refresh behavior.

## Concrete unresolved semantics

Do not claim the current UI is integrated solely because it can parse SSE:

1. The frontend session schema has `QUEUED/RUNNING/IDLE/FAILED`. Native approval,
   cancellation and uncertain-recovery states are richer. The current consumer
   ignores `session.awaiting_approval`, `session.cancelled` and `session.updated`.
   A complete presentation/approval contract needs an explicit decision; do not
   map approval waits to idle or cancellation to successful completion.
2. The current server enforces `projects.max` and `platform.web` using its
   entitlement configuration and transactional owner counters. Native Python
   plans currently specify request/token/model quotas, not equivalent project
   capabilities. Port the verified schema/policy and data migration deliberately;
   do not create unlimited projects or invent plan allowances for compatibility.
3. The frontend explicitly selects a model and effort, and has Plan/Build modes.
   Python currently chooses an entitled model by its own fallback selector and
   has no equivalent effort/Plan enforcement. Do not accept fields that execution
   silently ignores or invent provider capability metadata.
4. Frontend reads require a project ID, separate paginated messages and queue,
   saved files/history and distinct save/preview signals. Inline compacted model
   context cannot substitute for immutable transcript or filesystem snapshots.
   Current Python `GET /sessions/{id}` remains its native snapshot shape.

Evidence:
[frontend client schemas/routes](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/lib/backend/client.ts),
[event consumer](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/hooks/use-backend-session.ts),
[current project policy](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/projects/repository.ts),
[current model catalog](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/models/catalog.ts).

## Verification and review

`tests/test_session_events.py` adds 23 cases exercising shared repository,
authentication, HTTP envelope, CORS and ASGI boundaries. Reviewers reproduced
stale journal metadata and changed-identity writes during development; both are
fixed with regressions. Additional tests cover cursor scope and 100-record pages,
missing history, atomic rollback, privacy, token refresh classification, revoked
authorization, changing ownership, capacity, failed reads and stalled sends.

Reproduce from the task root (the full run includes all 23 new cases):

```powershell
& '.\.venv\Scripts\python.exe' '.\verification\run_offline_tests.py'
# 174 passed, 13 emulator cases skipped, 3 pre-existing warnings
```

The full offline run is saved as `verification/events-full-offline-pytest.txt`:
174 passed, 13 emulator tests deliberately skipped, and 3 pre-existing memory
datetime warnings. The separate emulator run and JUnit record are saved as
`verification/events-firestore-integration-pytest.txt` and
`verification/events-firestore-integration.junit.xml`. It uses the production
Firestore decorators with only a loopback demo project and anonymous credentials.
Final result: **13 passed, no skips**. The final admission race accepted 2 and
rejected 2 for capacity; the budget race accepted 3 and rejected 3 for quota.
Compileall, dependency consistency and diff checks passed. `app/worker.py` is unchanged.

Contention tests do not promise successful admission within the SDK's bounded
retries. Known ABORTED/retry-exhausted requests are checked for no partial state,
receipt, event or overspend. A rejected cancellation is explicitly retried as an
idempotent database command after contenders finish; no remote effect is retried.
Initial lock-timeout evidence is retained, not interpreted as provider success.
The shared emulator race helper also allows one explicit reconciliation attempt
for a known-aborted database-only command after all contenders finish. It never
retries unknown outcomes or external calls and does not change production retries.

Three independent read-only local reviewers audited contract, security and
recovery behavior. Outside this event slice they identified deadline checks
that wait for heartbeat enforcement, uncertain tool effects treated as ordinary
results, and oversized tool results persisted before a model-envelope check.
Those remain worker-guard-retaining engine work, not fixes claimed here. The
current TypeScript server already has generation-bound snapshot/recovery logic;
reuse it when porting durable workspace restore.

Subsequent local safety slice: the deadline/output/uncertain-effect findings
above are now addressed at shared cooperative boundaries, and legacy snapshots
share authorization/admission/cleanup with durable events. See
`EXECUTION_SAFETY.md` for current **240 offline + 21 emulator** verification,
review fixes and remaining process-termination/action-reconciliation limits.
The earlier counts and unchanged-worker statement describe the event checkpoint.

No live Firebase/Auth, E2B, provider, OAuth, billing, signed-in browser, deployment,
managed Firestore contention, compound-index or storage-limit validation occurred.
The experimental worker guard is unchanged.

## Decisions and conservative next steps

| Area | Existing frontend expectation | Native backend behavior / consequence | Smallest maintainable resolution | Input needed now? |
| --- | --- | --- | --- | --- |
| Lifecycle | Four session states; current UI clears stopping on idle/failed, with no approval surface | Native approval/cancel/recovery cannot be represented losslessly; a naive mapping hides pending actions or reports false completion | Keep canonical native states and the guarded worker; agree the client presentation contract before a projection or separately authorized UI port | Yes for how approval/recovery are presented; preserving states needs no further permission |
| Entitlements | Verified current server project/platform capabilities, owner counts, account catalog and usage | Python plan records only establish request/token/model limits; importing guessed project allowances can grant unintended paid access | Port the verified current schema through existing entitlement/admission modules; require authoritative configuration and a separately reviewed migration; missing policy fails closed | No new plan values need to be chosen for a faithful port; actual configuration and migration approval are prerequisites |
| Model / effort | Selected catalog model and supported effort remain fixed in a session | Python uses its own entitled fallback selection and has no effort contract; accepting ignored fields can execute the wrong model/settings | Reuse one shared resolver/entitlement boundary; persist and honor verified selection; reject unknown capabilities until configured | No product choice needed to honor existing selection; do not guess missing catalog/provider metadata |
| Plan mode | Explicit plan/build modes; existing server uses read-only tools in Plan mode | Python has no mode field or equivalent capability gate; accepting Plan as a prompt alone could mutate the workspace | Port the existing read-only tool-capability policy, persist mode and validate mode transitions server-side; reject unsupported Plan requests until then | No new product choice needed for the verified conservative policy; broader UX changes remain outside this task |

The Plan policy was verified at current server `src/runner/agent.ts` lines355-359,
1274-1277 and `src/runner/tools.ts` lines1075-1080: Plan uses read/list/search
tools; arbitrary command execution is deliberately excluded. Reuse these
invariants instead of implementing a second prompt-only planning path.

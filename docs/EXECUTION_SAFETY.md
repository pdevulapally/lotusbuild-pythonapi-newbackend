# Execution stopping and stream authorization

Subsequent process slice: `PROCESS_SUPERVISION.md` supersedes the cooperative
worker-only limitation below for Windows owned local execution/cleanup. It
preserves the remote-effect, memory, DB timing and manual-reconciliation limits.

Local slice, 2026-10-25, on foundation `6110155` plus the verified budget,
diagnostic and event patches. This supersedes the open engine findings in
`SESSION_EVENTS_CONTRACT.md`; it does not establish release readiness.

## Shared implementation

`RunRepository._authority_tx` checks lease/fence, run deadline and cancellation
at start, checkpoint, heartbeat, save and finish. Time is recomputed inside every
transaction attempt. A due deadline or cancellation grace commits recovery state,
cleanup intent and a lifecycle event, then raises outside the transaction. Raising
inside the callback would discard those writes. Uncertain run/sandbox capacity
stays reserved. Before grace expires, ordinary actions reject cancellation
immediately; finalization can record that cancellation.

The existing precedence is preserved: deadline expiry during cancellation grace
produces interrupted/error; elapsed cancellation grace produces cancelled.
Stale-running lease recovery still prefers cancellation whenever it was requested.
Both preserve recovery-required state and capacity. Tests pin the overlap.

`tools._invoke` is the single sandbox/virtual handler boundary. It bounds JSON
inputs and validates each existing tool schema before dispatch. Known invalid,
unsupported and disabled requests do not call a handler. Effectful handler
exceptions become typed uncertain outcomes; neither ordinary execution nor
approval resume converts them into a recoverable model tool result. No subsequent
tool/model call is started by that loop. The worker records a fixed reason and
server-owned tool name through existing failed-run finalization. Read-file and
screenshot observations, and the session-local plan update, are classified
separately; all other tools are conservatively effectful. This classification
does not grant approval, Plan-mode permission or external credentials.

`execution.limits` owns finite JSON, depth/node, signed 64-bit integer and size
checks. Tool inputs/results and cumulative batch results have a 32,000-byte
envelope; batches contain at most 32 calls. Command output is checked before
concatenation, and screenshot bytes before base64 encoding. Session admission
and worker saves check both the bounded JSON representation and actual Firestore
value encoding against 512,000 bytes. Oversized writes roll back; HTTP submission
rejection is 413 with `request/payload-too-large`. The limits deliberately stop a
run rather than silently truncate evidence or corrupt a tool protocol.

Native session responses add optional `recovery` metadata with fixed reasons
`tool_outcome_uncertain`, `output_limit_exceeded`, `run_deadline_reached` or
`cancellation_grace_expired`, and an optional tool name. Existing recovery-required
semantics remain authoritative; absence of this new metadata does not prove a
historical run is safe to retry. Native lifecycle states are preserved.

`api.streaming` owns authorization, admission, I/O bounds and response cleanup
for both durable events and the legacy snapshot stream. Both use the same token
verification function as `get_uid`, including revocation checking. Expiry is
checked before every emission; revocation/ownership has a one-second maximum
authorization lease anchored before verification and ownership reads. A check
that outlasts that lease fails closed. Owner checks also precede stream admission.
Snapshots compare the complete public payload, avoiding equal timestamp/message
count collisions. Both routes share the process limits of 100 streams/2 per UID,
bounded sends, iterator closure and idempotent slot release. The existing
`SSE_OPERATION_TIMEOUT_SECONDS` setting bounds I/O waits (default 10 seconds).

## Verification

From the isolated `backend` directory, using only synthetic provider keys:

```powershell
..\.venv\Scripts\python.exe -m pytest -q tests/test_execution_safety.py tests/test_stream_safety.py tests/test_session_events.py --tb=short
..\.venv\Scripts\python.exe ..\verification\run_offline_tests.py
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8787'
..\.venv\Scripts\python.exe -m pytest -q tests/integration/test_firestore_execution.py --tb=short -o junit_family=legacy --junitxml=../verification/safety-firestore-integration.junit.xml
..\.venv\Scripts\python.exe -m compileall -q app tests
..\.venv\Scripts\python.exe -m pip check
git diff --check
```

- Initial focused suite: **88 passed**; the final suite also adds the ASGI 2.3
  blocked-body variant alongside ASGI 2.4.
- Final full offline suite: **240 passed, 21 skipped**, three existing memory datetime
  warnings. Emulator tests are deliberately skipped in this run.
- Separate real SDK/loopback demo Firestore emulator: **21 passed, no skips**.
  The recorded invocation omitted `-o junit_family=legacy`, producing three
  reporting warnings about `record_property`; JUnit still records all 21 cases
  and race properties. The command above avoids those reporting warnings.
  Final admission race: two accepted/two capacity-rejected. Budget race: one
  accepted/five known-contention rejections. Those bounded SDK failures prove
  no overspend/partial state in this run, not guaranteed successful throughput.
- Compileall, dependency consistency and diff checks passed. The emulator was
  stopped after checking task-owned PID, executable path and start time.

Evidence is under task `verification/`: `safety-focused-pytest.txt`,
`safety-full-offline-pytest.txt`, `safety-firestore-integration-pytest.txt`,
`safety-firestore-integration.junit.xml`, emulator logs/process record and
`safety-installed-packages.txt`. No real providers, E2B, OAuth, Stripe or production
Firebase project were used. Tests cover ordinary and approval loops, late
deadline/cancellation races, rollback, uncertain effects, cumulative output,
malformed tool arguments, synthetic worker exit, stale stream authorization and
blocked-body cleanup. Synthetic exit is not an operating-system process kill.

Three independent read-only reviewers checked recovery, security and contracts.
Their findings led to signed integer bounds, tool attribution, schema-derived
argument validation, cancellation-overlap tests and authorization lease anchoring.
They found no remaining blocker in this bounded review. Shared BudgetLedger,
metered_create, authentication and repository state authority remain singular.

## Limits and next increment

- Deadline checks occur at transaction operation boundaries, not exact Firestore
  server commit time. A callback starting before the deadline can commit after it.
  Local clock correctness remains an assumption.
- The worker is still cooperative and runs handlers in its own process. A blocked
  SDK call/thread is not killed by a deadline, cancellation or an async I/O timeout.
  Bounds apply after SDK allocation; they do not cap peak SDK/process memory.
- An uncertain tool is stopped and marked for manual reconciliation. There is no
  durable per-action dispatch/result receipt ledger, automatic recovery procedure
  or exactly-once external-effect guarantee. Do not replay unknown outcomes.
- One-second stream reauthorization is a bounded cache, not instantaneous remote
  revocation. Process-local stream limits do not establish cluster capacity.
- Emulator results do not prove managed Firestore contention/index/size behavior
  or real provider timeout/termination behavior. No live integration is verified.
- Project/session/account alignment, lifecycle presentation, model/effort and
  enforced Plan mode remain separate contract work. Entitlements are unchanged;
  the budget envelope is not a proven monetary/tokenizer cap.

Next coherent increment: isolate a claimed run in a supervised, killable child,
propagate the existing fence/context, bound shutdown/output and test real
kill/restart without replay. Then add durable action receipts and explicit
operator reconciliation through the existing repository, followed by verified
workspace snapshot/restore. Keep the experimental worker guard and GitHub write
disablement until their separate readiness gates pass.


Subsequent receipt slice: `ACTION_RECEIPTS.md` adds durable per-tool intent,
one-time fenced permission and known-handler-result receipts, plus owner
inspection and unverified evidence recording. Unknown effects still cannot
replay/refund or release capacity. Trusted recovery resolution remains open.

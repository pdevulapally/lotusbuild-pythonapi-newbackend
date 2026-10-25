# Local backend checkpoint: 2026-10-25

## Checkout and scope

- Read-only inspection of `C:\Users\Preet\Desktop\lotusbuildbackendpython`
  found a clean `master` checkout at
  `bf3e59e9ac538d8143ca38250b0380dc8f9b42fe`. It was not changed or used to run tests.
- Work is isolated in
  `C:\Users\Preet\Documents\Codex\2026-10-25\task\backend`.
- GitHub's actual backend default branch is `master`; `main` does not exist.
  The fetched default is foundation PR1 merge
  `6110155b2936d74d15a6943c4755c34a3783aeac`.
- No repository-specific `AGENTS.md`, `.agents` skills, or `SKILL.md` were found
  in the backend source. LotusBuild product guardrails were read and applied.
- No frontend files, Desktop checkout files, remote refs, deployments, VPS
  settings, accounts, or production records were changed. No real credentials
  were created or read, and no billable provider operations were dispatched.

## Reviewed budget patch: verified and applied

The parent supplied the existing reviewed artifact
`lotusbuild-execution-budget-guards.patch`, Library identity
`libfile_42a063a09218819187d3d6b0d75feaa5`.
Its stated base matches this checkout. Its stated size is 57,560 bytes and
SHA256 is `808e3749daa2ed31f7fc77610c19aac384d4eb2c78d8282daabd1e076825c46d`.
The task-local file now matches both byte properties exactly.

The current Library resolved-reference helper was fetched unchanged with its
companion. Sandbox networking initially prevented transfer. The same supported
helper with approved network access reached metadata application, then failed:
`AttributeError: module 'os' has no attribute 'setxattr'`.
The temporary signed transfer manifest was removed after the attempt. The
parent then explicitly authorized the plain-text Library read route. Six
complete, contiguous line windows (1-1212) were joined as UTF-8 LF, and the
57,560-byte file's SHA256 matched before `git apply --check` and `git apply`.
No raw storage URLs, modified transfer helpers, or recreated code were used.
The original artifact's identity is retained alongside the local patch in
`..\patches\reviewed-budget-identity.json`; no Library item was replaced.

Library text inspection returned all 16 diff headers. They cover `.env.example`,
`API.md`, compaction, agent loop, Anthropic/OpenAI adapters, budgets/usage,
execution repository/slots, sandbox manager, execution-budget documentation,
and their tests. None overlaps this checkpoint's diagnostics code or new files.
The reviewed patch applied cleanly over the diagnostics changes. The combined
offline suite passed 151 tests. Budget implementation was transferred exactly,
not reconstructed. `app/worker.py` remains identical to the foundation base.

## Implemented slice: authentication and error diagnostics

The existing HTTP dependency calls Firebase verification with revocation checks;
HTTP routes share it. Raw verification exception messages previously appeared
in both the 401 response and application security logs. Missing Authorization
returned a framework 422 rather than the documented authentication failure.
Webhook verification failures also echoed exception text; generic errors logged
raw exception text and request paths; Redis failures logged raw exception text.

Changes are limited to these boundaries:

- `app/auth.py`: absent/empty bearer credentials return 401 with a bearer
  challenge before Firebase initialization. Failed verification uses a fixed
  response/log category. An unusable verified uid fails closed. Valid requests
  still use `check_revoked=True`.
- `app/billing/routes.py`: rejected signatures/payloads return a fixed 400 and
  log an event category. Verified event receipt handling is preserved.
- `app/main.py`: application security logging records the exception class,
  without exception text or raw request paths; the generic 500 stays fixed.
- `app/ratelimit.py`: Redis outage logging records the exception class.
  Existing outage behavior is preserved.
- `tests/test_security_diagnostics.py`: 17 regression cases exercise actual
  HTTP wiring and application logs with synthetic inputs and replaced services.

This reduces disclosure at the audited application logging/HTTP boundaries.
It does not establish that Uvicorn/access logs, SDK logs, every exception handler,
or tool-result transcripts are safe for every possible sensitive payload.
This independent diagnostics slice does not change execution budgets, approval
rules, worker guard, or job semantics. The separately supplied budget patch
changes admission/accounting as documented in `EXECUTION_BUDGETS.md`.

## Current frontend compatibility

Read-only source inspection used frontend `master` at
`8173d7c27f6bbb3c9fe040782d1a891219cffc45`, not the older Desktop frontend.
[Root page](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/app/page.tsx)
renders `ProjectsWorkspace` when signed in, and `LandingPage` otherwise.
Do not assume a `/tasks` replacement is the only frontend target. The direction
in the foundation `API.md` introduction is not evidence of current frontend
compatibility.

| Current frontend requirement | FastAPI foundation at this base |
| --- | --- |
| `GET /me`, `/me/usage`, `/plans`; catalog of models/efforts | No matching account/catalog routes |
| Create project, then `POST /projects/{id}/sessions` with prompt/model/mode/effort | `POST /sessions` takes task/kind, with no project record |
| Session includes projectId/model/effort/mode/messageCount, uppercase lifecycle | Different snake-case snapshot and lowercase lifecycle |
| Separate paginated `/messages` and `/queued-messages`; durable message identities | Transcript inline in session; no queue read API |
| `POST /sessions/{id}/cancel`, stable idempotency key | `DELETE /sessions/{id}` |
| `/events` with named events and resumable string `Last-Event-ID` | `/stream` emits whole snapshots without event IDs |
| Token-expired stream rotation and credential refresh | Identity checked when stream opens; no equivalent rotation |
| Saved project files, source, archive, history and diffs | No matching durable project/artifact APIs |
| Checkout body `{planKey}` | Checkout body `{tier_id}` plus required idempotency key |
| Desktop ticket and authenticated desktop relay | Direct stream URL surface, no matching ticket/relay contract |

Evidence:
[client schemas/routes](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/lib/backend/client.ts),
[session consumer](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/hooks/use-backend-session.ts),
[stream reconnection](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/lib/backend/session-events.ts),
[frontend migration inventory](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/MIGRATION.md).
This is source inspection, not authenticated browser/integration validation.

## Verification

Python 3.14.6; dependencies installed in the task-local `.venv` from the repo's
requirements. A resolved package snapshot is in
`..\verification\installed-packages.txt`; this is evidence, not a production lockfile.

From `task\backend`:

```powershell
& '..\.venv\Scripts\python.exe' -m pytest -q
# Unchanged foundation baseline: 104 passed, 3 pre-existing memory warnings.
& '..\.venv\Scripts\python.exe' -m pytest -q tests/test_security_diagnostics.py
# Diagnostics regressions after fix: 17 passed.
& '..\.venv\Scripts\python.exe' -m compileall -q app tests
git diff --check
& '..\.venv\Scripts\python.exe' -m pip check
# All passed; no broken package requirements.
```

From `task`:

```powershell
& '.\.venv\Scripts\python.exe' '.\verification\run_offline_tests.py'
# After diagnostics: 121 passed, 3 pre-existing memory datetime warnings.
# After exact budget patch application: 151 passed, same warnings.
```

The offline harness substitutes synthetic provider keys and denies Python socket
dispatch, allowing only stdlib socket-pair setup needed by Windows asyncio.
Its initial blanket socket denial broke Windows asyncio initialization; the
corrected harness passed. It is supporting evidence, not a network sandbox for
native libraries. Test output is saved in `..\verification\regression-pytest.txt`.
These offline tests replace Firebase/Firestore, Redis, model and sandbox
operations. The 151 cases reproduce the parent's 134-test combined foundation/
budget scope plus 17 diagnostics cases. Actual local Firestore SDK/emulator
coverage was then added separately; see `FIRESTORE_EMULATOR_TESTS.md` and the
JUnit evidence for results and limitations. No live Firebase/Auth, E2B, Stripe,
GitHub OAuth, deployment or authenticated browser verification was performed.

## Remaining priorities and next slice

1. The reviewed budget patch is reconciled and locally tested. Its byte envelope
   remains an unproven monetary ceiling; real-provider readiness is unchanged.
2. Local emulator coverage now exercises transactions and races. Observed
   contention can exhaust SDK retries or propagate a read ABORTED directly;
   bounded failure is tested without asserting successful admission under load.
   Production contention, compound indexes, and storage limits remain unverified.
3. Enforce tool capabilities and outbound policy at execution boundaries. Bash
   risk regexes and a disabled named GitHub-write tool cannot establish general
   authorization for arbitrary shell/network effects. Retain the experimental
   worker guard until that and provider/infrastructure readiness are demonstrated.
4. Add a durable external-effect ledger and reconciliation UX, plus filesystem
   snapshots. Current fencing prevents later writes/actions but cannot undo an
   already dispatched remote effect or restore an expired sandbox.
5. Define shared task, transcript, artifact and persistent-agent records before
   porting current frontend contracts. The current durable run is one turn in a
   session; per-user memory alone is not a persistent autonomous agent lifecycle.
   Inline transcript compaction is not archival, and document growth is bounded
   by the storage service rather than a durable transcript design.
6. Reconcile lifecycle, events, project persistence and account/billing contracts
   with the existing workspace, with ownership and negative-path contract tests.
   Do not manufacture successful saves/previews or weaken typed frontend checks.
7. Decide retention/deletion/export and user-controlled memory policies; audit
   long-stream reauthorization and all server/provider logging. Dependency
   locking, migration, recovery operations and staging failure injection remain.

This checkpoint is local development work. It does not qualify the replacement
backend for deployment or establish complete security/compliance.

The sequential implementation and audit checklist is
[`BACKEND_IMPLEMENTATION_TODO.md`](BACKEND_IMPLEMENTATION_TODO.md). Finish and
report each bounded slice before beginning the next; current frontend contract
alignment is next after this budget/emulator checkpoint.

## Subsequent contract slice

`SESSION_EVENTS_CONTRACT.md` records the completed local durable lifecycle event
slice: atomic journal writes, bounded owner-scoped `/events` replay, shared error
envelopes, authorization rechecks and stream cleanup. Native session/project
semantics above remain incompatible where explicitly listed; this is not a
complete frontend migration. Current tests and evidence are recorded in that
document and the checklist rather than treating the earlier 151/11 checkpoint
as new verification of later code.

## Subsequent execution safety slice

`EXECUTION_SAFETY.md` records the next completed local increment: shared deadline
checks, typed uncertain-effect stops without replay, schema-derived tool argument
validation, bounded tool/durable output, and shared legacy/event-stream
authorization and cleanup. Final verification: 240 offline passed with 21
emulator cases skipped, then 21 separate emulator cases passed. Killable process
supervision, durable action reconciliation and snapshot restore remain open;
live integrations and production readiness are not established.

## Subsequent process supervision slice

`PROCESS_SUPERVISION.md` records owned Windows run/cleanup spawning, bounded
claim-bound JSON RPC, terminal staging until validated child/containment exit,
independent stop monitoring, and conservative kill/restart recovery. Final
verification: 273 offline passed/24 skipped and 24 separate emulator passed.
Linux descendant containment, durable action receipts/reconciliation, workspace
restore and the remaining account/project/session/model/Plan contracts remain
open. The worker guard and GitHub write disablement remain in force.

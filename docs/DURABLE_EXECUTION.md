# Durable execution foundation (review branch)

This is an isolated development foundation, not a production-ready agent platform.
No production data migration or VPS changes are included.

## What this batch changes

- Session commands atomically commit the session, durable job and idempotency receipt.
  An API crash after commit does not discard queued work. Same-key/different-input
  reuse is a conflict, not a new action.
- Jobs are separate from the HTTP process. A worker claims queued work with an
  expiring lease and monotonic fencing token. Every agent session write checks
  lease ownership and cancellation in a Firestore transaction.
- Idle/approval results and the job's terminal state commit together. The UI does
  not see a ready-to-act state before the prior job has released authority.
- Approval submissions bind the current immutable batch ID and require exactly
  one decision for every risky call. Claiming the batch and queuing its job are
  atomic, including across requests with different idempotency keys.
- Cancellation is durable and checked before and after model calls and before
  every tool. It cannot undo a remote action already dispatched. After 15 seconds
  of cancellation grace (observed on a 10-second heartbeat), the worker fence is
  revoked even if its provider call is still blocked. Runs also have a 900-second
  execution deadline. These are development defaults, not an SLA.
- Cancellation queues sandbox cleanup. Workers retry failed cleanup; they never
  delete the registry merely because the provider could not be reached. The
  heartbeat can attempt cleanup while a model call is blocked. Existing external
  preview URLs may remain usable until provider termination, even though the API
  rejects new preview/desktop/screenshot requests for cancelled sessions.
- Unstarted leased jobs can be safely reclaimed. Expired *running* jobs are not
  automatically replayed: their external effects may have happened. They become
  interrupted/error (or cancelled), with `recovery_required=true`.
- Interrupted resource provisioning retains its cleanup intent even if no
  registry exists yet. A provider request could return late. An operator must
  reconcile that resource; there is not yet a management UI for this operation.
- GitHub writes are disabled both in tool advertisement and dispatch. The
  replacement needs a credential-isolated integration broker. Regex risk
  checks are not an authorization boundary and remain a separate release blocker.

## Local verification

Use a virtual environment, install `requirements.txt` and `requirements-dev.txt`,
then run:

```
python -m pytest -q
python -m compileall -q app
```

Offline durability tests use a lock-serialized, rollback-capable in-memory
transaction adapter, fake providers and the actual HTTP/agent-loop wiring.
They cover competing claims, stale fences, command/approval races, cancellation,
heartbeats/deadlines, cleanup retry and conservative crash recovery.
They do **not** prove Firestore transaction behavior, E2B termination, provider
SDK compatibility or production networking. A Firestore emulator/integration
suite and failure injection against staging are mandatory before deployment.

## Starting isolated development services

The API and worker are distinct processes:

```
uvicorn app.main:app --host 127.0.0.1 --port 4100
ENABLE_EXPERIMENTAL_WORKER=1 python -m app.worker
```

Do not run these with production credentials. The worker is intentionally
opt-in while remaining security and billing work is unfinished. Configure an
isolated Firebase project, restrictive origins/hosts, and development provider
accounts. `firestore.indexes.json` declares the worker queries' required indexes;
this patch does not deploy them. Missing indexes fail the poller, not silently
fall back to in-process execution.

A supervisor should restart failed/stuck worker processes. Lease fencing prevents
late session writes and new actions, but cannot forcibly cancel an arbitrary
blocking SDK call or reclaim its Python thread. Multiple workers/recovery polling
are needed for recovery when a whole process dies. All production provider calls
still need explicit network timeouts and provider-specific interruption.

## Data isolation and migration

New records use `agent_v2_sessions`, `agent_v2_jobs`, `agent_v2_commands`,
`agent_v2_cleanup` and `agent_v2_sandbox_registry`. Traces live under the versioned
session collection and include their run ID. Do not bulk-copy old `sessions`
records; their ownership/transcript schema is incompatible. The remaining
billing, account memory and connector stores are not yet fully versioned, so
**collection names alone are not permission to share a production database**.
Use a separate project until a complete migration is designed.

## Explicitly unfinished

- Separate immutable transcript, task and artifact records; inline session
  messages can still grow too large. Model context compaction is not archival.
- Durable filesystem snapshots and recovery after E2B sandbox expiration.
- Per-action external effect ledger, safe reconciliation and retry UX.
- Capability-enforced tools, network restrictions and production connector broker.
- Atomic usage reservations, sandbox concurrency limits and global cost ceilings.
- Token refresh/revocation during long streams, canonical resumable event IDs.
- Provider integration tests, emulator validation, dependency lockfile, deployment
  readiness checks, backups and rollback rehearsal.

Do not label this implementation exactly-once execution or fully secure.

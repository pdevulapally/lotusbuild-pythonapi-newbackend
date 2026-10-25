# Finite native next-turn queue

Later local continuation: TASK_AUTHORIZATION_CONTRACT.md adds explicit scoped
consent source, bounded account evidence and an opt-in trusted worker adapter.
It supersedes the no-grant-source prerequisite below, while automatic production
drain stays disabled. The finite routes described here retain their original
behavior; grant-scoped routes and immutable pinning are documented separately.

2026-10-25 local checkpoint in the same isolated backend checkout, preserving the
586-offline/62-emulator project/history checkpoint. This is an actual native
enqueue/list/cancel/authenticated-dispatch slice. **Automatic autonomous drain is
not enabled.** No frontend, Desktop original, live account/provider/billing/DB,
credential, deployment or system-security operation was performed.

## Source and deliberate boundaries

Frontend master was reverified unchanged at
`8173d7c27f6bbb3c9fe040782d1a891219cffc45`.
[Client contract](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/lib/backend/client.ts)
and pinned server `89a1bcac115e26a07c8990a3f491e84ee278b2ff`
[queue repository](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/sessions/repository.ts),
[routes](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/sessions/routes.ts),
[configuration](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/entitlements/store.ts)
and metering source were audited. Upstream queue capacity is the current plan's
configured build-meter hardLimit; it is not Python requests/day, project count or
page size. Upstream defines no queue TTL. Steering replaces a separate pending
instruction within a running lease; it is not a FIFO next turn.

The bounded native addition therefore requires caller-specified `expiresAt`,
rejects steering and mode changes, and accepts text only. No duration/default
retention, paid allowance or standing autonomous-task authority is invented.
The unchanged frontend does not supply this expiry or call the new enqueue route;
full frontend queue/steering/session integration is not claimed.

## Routes

| Route | Implemented native behavior |
| --- | --- |
| `POST /sessions/{id}/queued-messages` | 202 `{queued:true,queuedMessage}`; required Idempotency-Key and JSON `{prompt,expiresAt}`. Busy owned project/code sessions only. Prompt trimmed1..8000 with existing prose-control rules. Explicit offset-aware expiry must be future and within the verified request identity window. |
| `GET /sessions/{id}/queued-messages?limit=100&after=<id>` | Owned active FIFO rows and nextCursor, max200; technical pending bound100. Expired rows are excluded from active queuedMessages and counted in extra expiredCount. A retained cancelled/expired/dispatched ID remains a scoped stable cursor. Aggregate JSON512000-byte cap; reduce page size on413. |
| `GET /sessions/{id}/queued-messages/{item}` | Inspect retained state, explicit expiry and admitted runId when present. No bearer, UID or verification metadata in public response. |
| `DELETE /sessions/{id}/queued-messages/{item}` | Owned idempotent204; retains cancelled/expired record and command receipt. An already-dispatched item409s; this cannot retract a run or mutate an approved action. Use existing native run cancellation for that distinct action. |
| `POST /sessions/{id}/queued-messages/dispatch` | Required Idempotency-Key and newly verified owner identity. Dispatches one safe FIFO head into ordinary native SessionOut; expired head returns dispatched:false and retained expiry without starting work. Approval/cancellation/recovery/unknown effects/workspace locks/unverified completion409. |
| Existing `POST /sessions/{id}/messages` | Native `{content,mode?}` retained. It cannot jump ahead of pending FIFO items. Current-client `{prompt,steer,...}` remains422; no silent fallback or steering injection. |

Enqueue `steer:true`501s, explicit mode409s, nonempty attachments501s. Enqueue
accepts only queued/running sessions with current owner/project/selection,
transcript support and no cancellation/recovery/approval/restore lock. Existing
pending intents may remain while a later run pauses or fails; they can be
inspected/cancelled and cannot bypass those states. Queued text is not transcript
until admitted as a new user turn. Missing/foreign resources404 before cursor or
policy details. Malformed stored rows, missing heads and orphan receipts fail
closed rather than masquerade as empty history or reusable authority.

`dispatched` means a durable job was admitted, not that a provider executed,
an artifact was saved, or the task completed. Native lifecycle/approval/recovery
and all earlier compatibility blockers remain unchanged.

## One transaction and one runner

`RunRepository._submit_tx` is the existing submit body extracted once. Public
submit and queue promotion use it for current selection/Plan/project checks,
admission slots, native message context, immutable user text, session event,
ordinary operation=message job and command receipt. Queue consumption joins the
same commit, with no dequeue-then-submit gap. No second job runner/effect ledger
or expiring idempotency helper is introduced.

Private `queue_turn` command identities cannot collide with a public native
message key, even if the owner predicts a future queue ID. Finding an existing
private job/command/charge while the item is still pending is inconsistency503,
not permission to reset a fence, replay a job or skip its charge. Same-key lost
response replay returns the committed receipt snapshot; it never renews expiry,
re-adds the prompt or advances to the next item. Inspect current state separately.

`agent_v2_queued_messages` stores one retained record per accepted intent. Parent
monotonic sequences define FIFO regardless of wall-clock ties. Parent-owned
bounded pending IDs/head metadata share the session conflict boundary. Enqueue
does not increment the worker content revision. Child saves preserve current
queue/expiry authority, so a stale child cannot erase newer intents or widen a
deadline. Normal concurrent listing/cancellation/dispatch reads use one bounded
transaction snapshot. Events contain metadata only, never queued prompts.

The supervisor confirms quiescence **after verified child/containment exit**,
normal terminal save and finish. Queue dispatch additionally requires prior job
succeeded, completion_verified, session idle, no open actions/pending approvals,
no recovery/cancellation/restore and existing sandbox/run admission. Approval
waits never become idle and queue dispatch cannot change execution selections.

There are two commits between terminal publication and quiescence confirmation.
Parent death in that gap leaves no trusted confirmation: dispatch409s, default
queue discovery does not infer an exit certificate, and reconciliation is still
required. A queue that already has confirmation is discoverable after restart
through the existing worker loop. These are distinct crash windows; neither a
PID lookup nor an expired lease fabricates safe completion.

## Existing plan/budget authority

The current native entitlement resolver carries optional verified `meterLimits`
from the same selected plan. `config/registry.buildMeterKey` must reference one
declared COUNTER meter with DAILY/MONTHLY/NEVER reset period and exactly one
matching current-plan limit. includedAllowance/hardLimit are nonnegative safe
integers, included<=hard. Missing/ambiguous/invalid or paid-overage configuration
503s; no seed data, meter-key guess, new ENV variable or synthetic paid plan is
used. Queue capacity is min(configured hardLimit, technical100); technical100 is
a resource bound, not an allowance grant.

One `BudgetLedger.prepare_counter` boundary prepares the configured build charge
in the existing budget/reservation collections. When either registry or plan
meter data is present, every NEW project initial/immediate/FIFO message admission
must validate the complete pair and atomically consume one build. Approval
continuation consumes no new build. Partial configuration cannot disable the
counter. With neither configured, the earlier native model/request budget
contract is retained, but queue admission is unavailable. Existing exact model
entitlements and provider token/request reservations remain independent required
limits; capabilities cannot grant a missing native model.

No historical native/TypeScript usage reconstruction, billing migration, account
meter response parity, paid overage or counter refund is implemented. In particular
a dispatched run proven unstarted retains its recorded build consumption. Upstream
has scoped refund machinery; this bounded native restriction does not claim full
billing parity or charge/refund real money. Orphan charges and blocked/malformed
counter state require reconciliation; they never become a free new admission.

## Fresh auth and explicit finite execution

`get_queue_identity` reuses the existing shared signature/revocation verifier and
requires verified email plus finite iat/exp. Its one-second freshness window is
anchored before verifier I/O. The same proof is rechecked after transaction reads,
at shared admission and before returning the mutation. Expiry during reads or
buffered response construction rolls back the whole intent/job/counter commit.
These are application checks before commit; no hard database/network latency SLA
or zero-time revocation guarantee is claimed.

Only numeric original verified identity metadata and caller expiry are retained;
no ID token, refresh token or credential is stored/replayed. Token expiry bounds
this finite request authorization. It is not standing task authority. A renewed
login or repeated key does not silently revive an expired item. Fresh owner
dispatch can authorize the existing still-live finite intent, without extending
its explicit expiry. Existing ownership/current capabilities/model configuration
are re-read inside the dispatch transaction.

The dispatch job's deadline is bounded by expiresAt. Parent-owned expiry and root
identity carry across approval continuations, so approving an older action cannot
extend the original instruction window. New explicit user turns are separate
authorizations. An expiry after execution starts reuses the existing deadline,
uncertain-outcome stopping, receipts and cleanup authority.

If the current queue job/approval continuation is still only leased and has not
started, expiry or failed admission stops it without running sandbox cleanup.
Only its new run slot is released; the existing workspace, original text, root
history, approval/recovery evidence and build charge are retained. Native session
error/recovery reason `queued_run_not_started` explicitly requires reconciliation
before reusing that context; no fake successful idle state or automatic context
rewind is fabricated. Earlier root work may already have effects, so this says
only that the CURRENT run did not start.

## Exact automatic-dispatch prerequisites

`run_once(...,queue_authorizer=None)` defaults closed; main supplies no adapter or
enabling ENV switch. The real native owner-dispatch route works with fresh request
authentication. Synthetic injected readers test the real worker discovery and
atomic promotion boundaries; they are not production authority.

Audit found shared request auth/revocation and entitlement services, but no
configured background standing-task grant/status service. Installed Firebase SDK
verifier uses iat*1000 versus tokens_valid_after_timestamp and user.disabled.
Public get_user can read this status; its client uses a120-second default request
timeout with retries, and no whole-call bound was verified. It was not casually
wired into the parent or a transaction and no
live lookup occurred. Existing account counter metadata cannot establish revocation.

Automatic execution needs an explicitly authorized scoped durable task-grant
contract, fresh account/email/disable/revocation status binding the task, bounded
supervised reader I/O and verified integration with current entitlement/admission
checks. No such grant/credential was created here. A trusted future callback must
return a fresh bound identity, not a cached UID/old claims or a fabricated renewal.
The finite native background test hook also binds original iat/exp. One denied
candidate cannot starve another owner in the bounded scan. Expired or unauthorized
pending intents stay retained; owner inspection/cancellation/fresh dispatch is
required, not an unapproved automatic TTL deletion.

## Verification and release limits

Queue artifacts live under task-7/queue; final totals, exact patches, hashes,
review confirmations and emulator stop evidence are in NEXT_TASK_HANDOFF and the
queue checkpoint manifest. Tests cover repeated keys, private/public namespace,
FIFO/tombstone cursors, owner opacity, cancellation races, original text/counter
rollback, late auth expiry, orphan artifacts, configured build charging, current
capability loss, bounded approval continuation, retained unstarted workspace,
lost replies, restart discovery, blocked confirmation gap and actual spawned
child-exit gating. Emulator uses real SDK transactions with AnonymousCredentials,
demo-lotusbuild-local and numeric loopback8787 only.

The real index/prior bytes stay hash-preserved; no reset/reclone/clean occurred.
Retention/export/erasure policy must include queue records and duplicated receipt
snapshots before production. No TTL/deletion is deployed. Worker guard, disabled
GitHub broker, native lifecycle, Plan/effort gates, quarantined restore and noTunnel
remain. Existing account/catalog/frontend/restore/provider release blockers remain;
no scheduler or next phase is started by this slice.

# Scoped native task authorization: local foundation

2026-10-25, same isolated checkout, after the verified636offline/71emulator
native queue checkpoint. This is new native source, synthetic HTTP fixtures and
local emulator verification. No real grant, account lookup, token, connector
permission, paid call or autonomous production worker was created/enabled.

## Exact source boundary

Audited existing shared Firebase request verification, installed Firebase Admin
account/revocation implementation, native entitlement/model resolution, durable
run/queue/approval receipts and BudgetLedger. Existing request verification is
not persistent task authority. Installed default Firebase Admin get_user uses
request timeout/retries which do not establish a short whole-call deadline.
The new reader runs that public SDK lookup in existing owned process containment;
tests use synthetic handlers exclusively. No parallel authentication, runner,
paid entitlement store or commercial plan is introduced.

Frontend master was verified unchanged at
`8173d7c27f6bbb3c9fe040782d1a891219cffc45`; authoritative pinned TypeScript server
remains `89a1bcac115e26a07c8990a3f491e84ee278b2ff`. Neither defines this durable
native grant protocol. This addition does not claim client grant/queue parity.
The frontend and original Desktop checkouts are untouched. Explicit approval,
cancellation and recovery UI states remain the chosen later frontend direction.

## Native HTTP contract

All mutations require the existing Idempotency-Key (1..200 characters) and
Firebase bearer authentication; IDs are owner/session scoped. Foreign/missing
resources404 before resource details. Strict JSON rejects unknown fields.

| Route | Implemented behavior |
| --- | --- |
| `POST /sessions/{session}/task-authorizations` | 201 immutable explicit consent. Requires every scope/bound field below, fresh verified default-namespace owner and bounded current account evidence. Missing policy/reader503; unsupported scope403/422. |
| `GET /sessions/{session}/task-authorizations/{grant}` | Current retained grant, effective_status and cumulative resource_usage; read-only owner access. No current reader/plan/policy dependency. Does not assert unconditional execution eligibility. |
| `POST /sessions/{session}/task-authorizations/{grant}/revoke` | Expected-version JSON `{expected_version}`; atomically revoke/increment once and retain tombstone. Version conflict409; same key/input replays original response. Does not require policy/reader/entitlement to withdraw consent. |
| `POST /sessions/{session}/task-authorizations/{grant}/queued-messages` | 202 existing queue admission with exact immutable grant/version pin. JSON `{prompt,expiresAt}`; explicit item expiry cannot exceed consent. Owner freshness uses configured task policy. |
| `POST /sessions/{session}/task-authorizations/{grant}/dispatch` | Fresh owner request admits only the current FIFO head carrying this exact grant; mismatch409. Known response replay is resolved before consulting a newer head/reader. Grant ID has its own receipt namespace. Ordinary run admission remains authoritative. |

Consent fields: `mode`, exact `model`, `provider`, required nullable `effort`,
`operations` containing exactly `queue.dispatch` and `run.execute`, nonempty
`tools`, offset-aware `expiresAt`, and positive integer `max_turns`,
`max_run_seconds`, `max_model_requests`, `max_reserved_tokens`. No defaults or
implicit renewal. Exact current project/session/model/provider/effort/mode are
pinned; a grant cannot bind another task/project, broaden mode, edit approved
actions, rebind an existing queued item, or bypass existing selection/Plan policy.
Consent input expiry becomes numeric scope.expires_at in stored/public grant;
scope and other native grant properties use snake_case. Inspection includes
status (active/revoked), effective_status (also expired), uses, version and
resource_usage.{requests,reserved_tokens,actual_tokens}. Public responses expose
no UID, original consent iat or account evidence. No bearer or refresh credential
is stored or replayed as background authority.

The original finite queue route optionally accepts taskAuthorizationId but keeps
its existing one-second fresh-request proof. The dedicated task routes use the
explicit configured task freshness window, anchored before shared verification
I/O. Actual spawned cold-reader consent/enqueue/dispatch/start is tested through
these HTTP routes. No proof clock slides after slow I/O or transaction retries.
Request token expiry never supplies standing authority; durable dispatch checks
the grant's original consent iat against current account revocation facts.

## Required configuration and trusted composition

No production policy is seeded. Required new native document
`config/task_authorization` field names:

- schema_version, version, max_duration_seconds, max_turns, max_run_seconds,
  max_model_requests, max_reserved_tokens
- reader_timeout_seconds, evidence_ttl_seconds
- allowed_modes, allowed_models, allowed_tools

Schema1 enforces technical maxima: run bound900seconds; evidence TTL at most
30seconds and positive reader timeout shorter than TTL; positive exact integer
execution/resource bounds. These are safety caps, not commercial/user defaults.
The configured policy plus each explicit owner request must supply actual values.
Policy version changes invalidate old grants; there is no mutable grant-scope,
renew/rebind or agent self-escalation endpoint. Current plan/model/AI/platform
entitlements and configured build meter must independently allow every admission.

V1 tools are limited to implemented read_file, write_file, update_plan, further
narrowed by current mode/Plan policy, tool registry and existing risk/approval
receipts. Shell/browser/computer/connector/communication/deploy operations are
unsupported. A configured unsupported tool fails503; it is not an empty success.
The agent has no grant-management tool/RPC. Assistant/tool content cannot consent.

`RunRepository(account_reader=None)` and `run_once(queue_authorizer=None)` remain
the defaults; main supplies neither. Trusted source composition would explicitly
construct BoundedAccountReader with required max_concurrent and install
TaskQueueAuthorizer on that same repository. No enabling ENV switch is added.
This task did not perform that production composition or create real grants.

## Bounded fresh facts and revocation

The reader returns only uid, disabled, email_verified, valid_after_ms. Evidence
has original pre-I/O wall and monotonic deadlines, is strict/schema checked and
requires clean owned child exit/reap. Cached evidence retains that original
deadline. Reader capacity is explicit per-instance1..16 with nonblocking
admission and a technical100-entry memory bound; this is not a cluster quota.
Timeout, crash, wrong UID, stale evidence, unavailable lookup or capacity denial
fails closed without exposing SDK diagnostic text. Actual contained-process tests
exercise normal, blocked, crash and private-error handlers on this Windows host.

Firebase semantics are preserved: consent_iat*1000 below current
tokens_valid_after_timestamp revokes; equality remains valid. Disabled/unverified
accounts fail closed. Shared verification explicitly rejects nondefault Firebase
tenant claims; multi-namespace account support is not implemented. Inspection and
withdrawal retain current shared request authentication, including this guard.

Fresh account/grant/current policy checks occur at dispatch, start, parent run
checkpoints/heartbeat/action permit/save and approval continuation. Model reserve
uses parent-stamped unexpired facts plus current grant/policy/job/session in the
existing reservation transaction; provider execution has a post-reservation
parent checkpoint. Revocation takes effect by the next boundary, subject to the
configured evidence TTL. It does not claim an already issued remote effect was
undone. Existing uncertain receipts/recovery continue to hold execution.

## Transaction, expiry and accounting guarantees

Consent creates grant, initialized per-task row in existing BUDGETS_COLLECTION
and ordinary scoped receipt atomically. An orphan/missing/corrupt task budget
fails closed and cannot reset a ceiling. Consent replay returns its original
receipt without resurrection, even after revoke. Inspect reveals current state.
Revocation remains possible when the reader/policy/budget is unavailable.

Queue grant-use consumption, immutable item/version, ordinary shared _submit_tx,
job/slots/original message/build charge/event/receipt commit together. Restart
discovers the durable item; no login token is retained/replayed. Existing finite
items cannot be upgraded and cancelled/expired items retain queue tombstones.
Only a verified quiescent prior run permits dispatch; no concurrent workspace
runner or effectful interruption. FIFO and private receipt namespaces survive
competing owner/worker attempts and lost replies. Pending items remain cancellable
after revoke, but cannot run under another grant/new login.

Task model-request and reserved-token bounds are cumulative across turns,
approval continuations and compaction. They join the SAME BudgetLedger reserve
transaction as paid limits/model quota; settled tokens do not replenish consent.
Settlement after revoke records actual tokens and remains possible. Actual usage
overrun blocks future task use; no refund or implicit new entitlement. Final
freshness/lease/deadline checks occur after all transaction reads and before
return; retry cannot authorize from an old sampled timestamp.

The task run wall deadline is min(existing limits, original item expiry,
first-start plus explicitly consented max_run_seconds). Approval inherits this
same root deadline rather than opening a new window. A denied/expired task still
only leased and never started releases only its new run slot, preserves the prior
workspace and records queued_run_not_started reconciliation. Stale claims cannot
release capacity or mutate state. Running uncertain outcomes retain existing
receipt/fencing/cleanup semantics; no false completion/retry is introduced.

## Verification and remaining gates

Final evidence is recorded in task-authorization-checkpoint-manifest.json and
NEXT_TASK_HANDOFF.md. New focused suite50passed, nine real-SDK emulator cases
passed, and all three independent read-only source reviewers cleared the bounded
slice. Final686offline passed (80emulator skipped); separate80emulator passed.
Compileall/pip check/diff check passed. Exact-owned emulator stopped, port8787
bindable. Private-index patch reproduction and preserved baseline/index checks
are certified by the checkpoint manifest; see handoff/artifact hashes.

Automatic production drain remains disabled. Production still needs separately
authorized policy values and trusted reader/worker composition, configured
current commercial entitlements/build meter, real isolated auth/provider/E2B/
billing E2E, service/cluster capacity validation and Linux containment proof.
No actual grant/credential/connected-service permission was created here.
Retention/export/revocation audit policy must cover grants, queue records,
account evidence and receipt snapshots; no real deletion/migration/TTL added.
Existing unconfirmed-exit reconciliation, stopped-context recovery, effort/paid
ceiling configuration, account/catalog mapping, workspace adoption and frontend
lifecycle/queue integration remain blocked as previously documented. Scheduler,
notifications and mid-run steering are outside this slice.

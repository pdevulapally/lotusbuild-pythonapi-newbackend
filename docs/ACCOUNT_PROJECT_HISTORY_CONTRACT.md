> Queue continuation supersedes the historical queue501 row below. Read
> [DURABLE_QUEUE_CONTRACT.md](DURABLE_QUEUE_CONTRACT.md) for working native owner
> dispatch and exact remaining automatic/expiry/frontend boundaries. Prior586/62
> checkpoint evidence remains unchanged.

# Account, project and text-history contract checkpoint

Local bounded slice, 2026-10-25. This is partial current-client alignment, not a
replacement deployment or full account/session UI integration. Native lifecycle,
Plan/model selection, leases/fences, supervision, receipts and recovery remain
authoritative. No providers, billing, live Firebase, real credentials, remote
writes, migrations or original Desktop/frontend edits were used.

## Verified source and policy

GitHub frontend `master` was verified unchanged at
`8173d7c27f6bbb3c9fe040782d1a891219cffc45`. Authoritative current client schemas and
calls are in [lib/backend/client.ts](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/lib/backend/client.ts).
Pinned TypeScript server source
`89a1bcac115e26a07c8990a3f491e84ee278b2ff` was read for
[project routes](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/projects/routes.ts),
[project authority](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/projects/repository.ts),
[session authority](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/sessions/repository.ts)
and typed entitlement schema/service. Task-7 `sources/` retains the inspected
client and project/session files. The pinned
[build admission policy](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/entitlements/build-admission.ts)
also supplies execution-deny semantics. No legacy runtime was imported or run.

Verified project rules are fixed `web` platform, verified email on creation,
printable trimmed names up to 100 characters and description up to 500,
`platform.web` boolean and numeric `projects.max`, owner counter admission and
an exclusive project workspace guard. Missing guard is different from explicit
null. Source seed allowances are not runtime authority and were not copied.
Python tier names/model quotas differ; no tier mapping or paid grant was invented.

The existing native `resolve_entitlements` still selects the current plan and
paid-through subscription eligibility. Its `Entitlements.capabilities` carries
optional `plans/{tier}.entitlements` in the verified source shape:

```json
{"capabilityKey":"platform.web","value":{"kind":"boolean","value":true}}
```

`projects.max` uses `kind: number` and a nonnegative safe integer. No default
allowance is provided. Missing/malformed/duplicate required configuration returns
503 `project/policy-unavailable`; an explicitly false platform or exhausted cap
returns 403. Existing seed plans remain unchanged and cannot create projects
without verified configuration. Setting this optional data does not enable any
missing account/catalog/effort/provider capability. It does not grant GitHub writes.

Project-bound execution additionally requires typed boolean `ai.run` and
`platform.web`, and typed `ai.model.allowlist` (`stringList`). Missing/invalid
execution configuration503s; explicit AI/platform/model denies403. The verified
empty-list meaning is unrestricted by that capability; the native model list
still independently must entitle the exact requested model and its quota. No
second catalog or subscription resolver exists. Project sessions require an
explicit native model. Project count limits apply on creation, not as a guess at
execution quotas. Existing fenced checkpoints recheck current project execution
capabilities, including immediately before provider spend and after reservation.
Revocation retains uncertain reservations; it cannot trigger a refund or call.
Failed finalization, cancellation and cleanup remain permitted after revocation.

## Implemented routes and exact boundaries

All routes use existing shared Firebase bearer/revocation verification, error
envelopes and rate-limit services. New writes use existing repository transaction
and command-receipt authority. CORS adds PATCH only; origins and headers stay as
previously configured.

| Route | Local contract |
| --- | --- |
| `POST /projects` | Current-client project shape, HTTP201; verified email, required Idempotency-Key, transactionally rechecked current capabilities/count. Same key replays the original response snapshot. Changed normalized input409. |
| `GET /projects?limit=25` | Owned newest-first list, max100. Additional `before=<project-id>` and `nextCursor` permit older pages. Owner-scoped cursor; gaps/corruption fail closed. |
| `GET /projects/{id}` | Current-client metadata shape; missing/foreign404. |
| `PATCH /projects/{id}` | Narrow name/description patch in one current-state transaction. Empty description removes it. Cannot change owner, guard, platform, state or capabilities. No run revision mutation. |
| `GET /sessions/{id}/messages?limit=100` | Current-client text rows `{seq,role,content,createdAt,attachments}`. Newest max200 returned in ascending seq. Additional `before=<seq>`/`nextCursor` navigate older rows. Aggregate page JSON <=512000 bytes; oversized413 without a cursor advance. Reduce limit to read a smaller page. |
| Existing `POST /sessions` | Existing native `task/kind` contract retained; optional `project_id` now binds a fresh code workspace, with verified-email gate and transactional ownership/policy/guard. Native explicit model/effort/Plan rules retained. |
| `POST /projects/{id}/sessions?native=true` | Explicit native extension using current-client prompt/model input; returns public SessionOut, HTTP201. Attachments unsupported501. Explicit effort still503. A missing effort remains unset, never fabricated as standard. |
| `GET /projects/{id}/sessions?native=true` | Explicit native extension, max100, exact expected owned binding required. Missing/dangling/corrupt binding503, never empty fake success. |
| `POST /sessions/{id}/cancel?native=true` | Explicit native extension; required Idempotency-Key and existing cancellation authority. Atomic receipt snapshot replay cannot recancel or overwrite newer state. |
| Existing `GET /sessions/{id}`, snapshot/event streams, approvals and native message POST | Remain native. Rich lifecycle is unchanged; see SESSION_EVENTS_CONTRACT and ACTION_RECEIPTS. |

Each new project uses native `agent_v2_projects`; its counter uses
`agent_v2_accounts`. These own only this new namespace, not historical TypeScript
`users/projects/sessions` data. No imported-data adoption or counter reconstruction
occurs. Projects are private with public preview disabled. There is no sharing,
public preview or recoverable-delete implementation in this slice.

Project-bound execution is restricted to one code session/workspace per project
for this foundation. Its pointer is set atomically with session/job/slot/receipt/
initial transcript/event creation and is **not released on idle, approval,
cancellation or recovery**. A second session409s even after idle. Follow-up turns
reuse the first session through existing native commands; ownership/current
project permission rechecks occur on admission/start/action boundaries. Failed
finalization, cancellation and cleanup remain possible after capability revocation.
This restriction protects user work until verified persistence/adoption and release
semantics exist; no saved filesystem is falsely claimed to resume.

## Durable original text, separate from model context

`agent_v2_messages` is create-only immutable text history on the same transaction
as session/event/receipt state. User text is appended during command admission.
Assistant text uses an explicit bounded append signal, checked against the exact
current context prefix under the live claim/revision/fence. Tool inputs/results,
system prompt, source payloads and compaction summaries are excluded. Sequence
begins at zero and `message_count` is parent-owned. Existing action and workspace
authority remains parent-owned too; changing project binding/history metadata in
a child save rejects rather than overwrites it.

Compaction uses a separate signal; parent verifies original first message and
retained tail. Its summary cannot replace original text rows. Signals are consumed
by both parent save and child RPC acknowledgement. Supervision still stages native
terminal data until validated child/containment exit. No alternate runner or
effect ledger exists. Text emitted before a later run failure remains honest text
history, not a success signal. Empty tool-only assistant messages create no UI row.

Pre-upgrade sessions have no authoritative original transcript. GET messages503s
instead of reconstructing one from compacted context. Missing/foreign sessions404
before cursor/history details; missing rows, wrong ownership or discontinuity503.
The event journal remains metadata-only. Index declarations were added locally,
not deployed. Transcript/counter/receipt data has no automatic TTL or deletion.
Approved privacy purpose, retention, access/export and deletion policy must include
these collections before production adoption; code does not establish compliance.

## Explicitly blocked contracts and product decision

| Route/feature | Honest result/reason |
| --- | --- |
| `GET /me` | 503 `account/contract-unavailable`: verified catalog display names, provider effort capabilities, paid effort ceilings and account meter-limit mapping are absent. No synthetic plan, empty model list or fabricated grant. |
| `GET /me/usage` | 503 `account/usage-unavailable`: mapping from existing budget authority to client meter schema is unimplemented. No zero-usage success and no claimed native HTTP usage API. |
| Default new project session create/list and POST cancel | 503 `session/client-contract-unavailable` before mutation: four-state schema and configured default effort are insufficient. `native=true` is explicit local API extension, not frontend parity. |
| Current frontend `POST /sessions/{id}/messages` body `{prompt,steer,attachments}` | Existing native route requires `{content,mode?}`; current-client body422s. No silent queue or steering acceptance. |
| `GET /sessions/{id}/queued-messages` | Owned501 `session/queue-unavailable`. No queue admission/consumer policy exists. |
| `PATCH /sessions/{id}` | Owned501 `session/update-unavailable`; arbitrary status/approval/recovery rewrites are never accepted. |
| Project DELETE | Owned501 `project/deletion-unavailable`; recoverability/retention/authorization not defined. Existing DELETE session remains native cancellation, not data deletion. |
| Project files/content/changes/archive reads | Owned501 `project/content-unavailable`; verified persistence/adoption adapters remain missing. No fake empty history/files/archive. |
| Attachments, account erasure, shared/public workspaces, billing/catalog/schema parity | Not implemented by this slice. No broad TypeScript account-erasure or family-policy parity claim. |

**Recorded UX decision:** A later frontend slice should add distinct approval,
cancel-requested/cancelled and recovery-required presentations/actions. The parent
supplied this answer in the user's authorized task-management flow. No frontend
edit or production permission is implied. Unsupported legacy projections reject
now. Native `awaiting_approval`, `cancel_requested`,
`cancelled` and `recovery_required` are not translated into successful idle or
plain failure. Native queued/running/idle/error mappings alone do not close the
client contract, because model/effort/project fields are also required.

## Verification and production gates

Task-7 holds exact baseline hashes, private-index cumulative/slice patches and
JUnit evidence. Three independent read-only reviewers cover contract fidelity,
tenant/auth/concurrency and maintainability/regression; one editor applies changes
with baseline/applied hash guards. Real Git index and all unrelated baseline bytes
are preserved. Full offline and real-SDK emulator results are recorded in the
latest NEXT_TASK_HANDOFF and task-7 checkpoint manifest. Tests assert source-derived
HTTP fields and errors; no frontend build/browser or live integration was run.

New coverage includes project cap/repeat races; schema/ownership/input denial;
current policy revocation; narrow concurrent metadata patches; exclusive project
bindings; missing/corrupt binding rejection; newest/older history pages; immutable
transcript rollback and compaction; stale child metadata; actual spawned RPC
append/compaction/terminal completion; cancellation snapshot races. Emulator runs
use only AnonymousCredentials, demo-lotusbuild-local and numeric loopback8787.

Keep the experimental worker guard, production GitHub broker disablement and
quarantined local-only workspace restore. Missing effort inputs remain closed.
No tunnel, deploy, production DB operation, migration or real deletion is approved.

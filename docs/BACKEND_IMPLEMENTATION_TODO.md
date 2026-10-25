Latest CI review checkpoint (2026-10-25): local737offline+80emulator passed,
51pipeline regressions included. Fresh Windows hash-lock install, extracted
Windows synthetic liveness, pip-audit fixed-lock and actionlint passed. Linux
execution/hosted CI/live integration remain UNRUN; production promotion is
hard-blocked and no deployment job/credentials are wired. User explicitly
authorized review-branch publication only: codex/fastapi-autonomy-foundation-20261025.
Master/production remain unchanged. No next implementation stage is authorized.
Final remote commit/link and publication proof are reported by this task.

---

# Backend implementation checklist

Read-only product/delivery audit completed: TEAMS_CONNECTIONS_DELIVERY_AUDIT.md
records current8173frontend/pinned89a1server, existing family/sharing/skills,
UID billing and incompatible catalogs, exact blocked frontend routes and old
CI/CD pattern. No runtime changes. Dynamic/smooth/fast/frontend-compatible/
deployable are acceptance targets with evidence gates in that audit, not claims
from686offline/80emulator counts. Next requested stage is LOCAL CI/release
validation preparation; no pipelinefiles or publication in this audit.

Scoped durable task authorization continuation: native source now supplies
explicit finite owner consent, inspect/revoke, pinned queue/worker adapter and
bounded fresh account facts. All source paths reuse current auth/admission/
Plan/receipts/BudgetLedger. Actual access grants and production composition remain
disabled; see TASK_AUTHORIZATION_CONTRACT.md and latest handoff for verification.

Finite native queue continuation: actual enqueue/list/inspect/cancel/fresh-owner
dispatch now implemented and locally verified636offline/71emulator. Automatic
autonomous drain defaults closed pending authorized scoped durable task grants
and bounded fresh account/revocation reader. See DURABLE_QUEUE_CONTRACT.md for
finite expiry, original approval deadline, retention, configured counter, quiescence
crash gap and frontend compatibility restrictions. No steering/scheduler started.

Account/project/history continuation: verified project metadata/ownership/counter
and immutable text history are implemented locally (586 offline +62 emulator).
Full account/client session compatibility remains blocked; see
`ACCOUNT_PROJECT_HISTORY_CONTRACT.md` for exact mapped/blocked routes. Typed plan
AI/platform/model denies are enforced at existing checkpoints. Product direction
is to add explicit approval/cancellation/recovery UI states in a later frontend
slice; no frontend changes belong to this backend slice.

Selection/policy continuation: exact configured native-model requests and enforced
Plan capability gates are implemented locally; see `MODEL_PLAN_CONTRACT.md`.
Explicit effort is still blocked until verified provider capabilities and the
existing paid effort ceilings are supplied. Do not infer them from seed tiers.
This does not close full frontend account/catalog/project compatibility.

Updated 2026-10-25. Work one bounded slice at a time. Implementation, local
verification, and live verification are separate states. This checklist is not
authorization for deployment, migration, credential grants, paid calls, or real
notifications. Desktop/backend originals and the frontend remain untouched.

## Reliable core engine, in order

| Item | Implemented | Locally tested | Live verified |
| --- | --- | --- | --- |
| Foundation: transactional commands/jobs, leases/fencing, cancellation, atomic approvals, browser-bound OAuth | Merged `6110155` | Baseline 104 tests | No |
| Authentication/error disclosure fix | Complete locally | 17 new regressions; combined suite below | No |
| Reviewed execution-budget patch | Exact artifact verified/applied | 151 combined offline tests | No |
| Real Firestore transaction/emulator coverage | Local test suite added | See `FIRESTORE_EMULATOR_TESTS.md` and saved JUnit evidence | Local emulator only |
| Current frontend account/project/session/event contract alignment | Durable event transport and shared errors implemented; projects/session shapes remain open | 23 new event cases; 174 offline passed; 13 emulator passed separately | No |
| Shared deadline/output/uncertain-effect stopping and both-stream authorization | Complete bounded local slice; action reconciliation remains open | Earlier checkpoint 240 offline + 21 emulator; see `EXECUTION_SAFETY.md` | No |
| Owned execution/cleanup process supervision and crash recovery | Windows local slice implemented; Linux descendant containment unverified | 273 offline passed, 24 emulator skipped; separate 24 emulator passed; see `PROCESS_SUPERVISION.md` | No |
| Durable action receipts and reconciliation evidence | Parent-owned intent/permit/result ledger and owner unverified evidence implemented; trusted resolution remains a dependency | 309 offline passed + separate 35 emulator passed; see `ACTION_RECEIPTS.md` | No |
| Exact configured model selection and enforced Plan mode | Explicit native plan model pinned; shared resolver, parent Plan capability gates and mode persistence. Effort blocked by missing verified capability/paid ceiling configuration | See `MODEL_PLAN_CONTRACT.md` and task-4 final evidence | No |
| Workspace snapshot restore | Bounded manifest/integrity and fenced publication/quarantined materialization foundation; production adapters/adoption remain blocked | 65 new local cases plus 8 real emulator cases; see `WORKSPACE_CHECKPOINTS.md` and task-5 evidence | No remote workspace restored |
| GitHub credential broker and bounded write execution | Disabled trusted-server foundation with typed named-branch PR approval, scoped credential lease, safe native REST client and existing receipts; production adapters/policy remain absent | 102 new offline cases + 6 emulator cases; final 524 offline and 52 emulator passed; see `GITHUB_WRITE_BROKER.md` | No |
| Isolated Firebase/E2B/OAuth/billing end-to-end validation | Pending dedicated test setup/authorization | No real external integration validation | No |
| Monitoring, backups/rollback and release readiness | Audit/implementation pending | Pending | No |

- [x] Inspect original checkout read-only, create isolated checkout, verify current upstream.
- [x] Reconcile the exact budget artifact by size/hash, preserve diagnostics work,
  run combined regressions, retain `ENABLE_EXPERIMENTAL_WORKER` guard.
- [x] Add actual SDK/emulator tests; distinguish bounded contention rejection
  from successful admission, without unbounded retries or provider replay.
- [ ] Align the backend with current frontend `8173d7c` contracts, beginning with
  account/catalog and project/session ownership and explicit lifecycle schemas.
  Inspect the existing current server first; reuse verified rules. Port one
  coherent operation and its negative tests before moving to another.
- [x] Add atomic lifecycle journaling, owner-scoped resumable `/events`, shared
  client error envelopes, CORS replay headers, authorization rechecks and bounded
  stream cleanup. Preserve explicit cancellation/approval/recovery semantics.
  See `SESSION_EVENTS_CONTRACT.md`; this is partial frontend compatibility.
- [x] Add paginated durable original text transcript (latest200/older pages;
  compaction and spawned RPC safe); native queued messages added below and
  distinct mid-run steering remains501. See ACCOUNT_PROJECT_HISTORY_CONTRACT.
- [x] Implement bounded native next-turn queue enqueue/list/cancel/owner dispatch;
  preserve FIFO, expiry, approvals/receipts and workspace. See DURABLE_QUEUE_CONTRACT.
- [x] Implement LOCAL scoped task-consent source and bounded account/revocation
  reader using shared native queue/admission/receipts/budgets; synthetic grants only.
- [ ] Separately authorize production task policy/composition and isolated real
  E2E before enabling automatic drain. Reconcile unconfirmed exit and stopped
  contexts, approved refunds/retention, then client queue UX.
- [ ] Define safe distinct mid-run steering contracts; resumable
  lifecycle events and bounded stream reauthorization are already implemented.
  Keep agent completion, saved
  artifacts, preview readiness, cancellation and uncertain recovery distinct.
- [ ] Verify timeouts for every external boundary and process supervision under
  blocked calls. Extend existing lease/cancellation behavior rather than adding
  a parallel runner. Add crash/kill/restart fault injection.
- [x] Enforce deadlines at shared action checkpoints and durable writes, stop
  uncertain effects without replay, and bound tool/session output. Harden legacy
  snapshot-stream authorization through shared token/admission/I/O services.
  See `EXECUTION_SAFETY.md` for tests and precise timing/allocation limits.
- [x] Add supervised killable run/cleanup processes with actual Windows
  blocked-call, crash, parent-kill/restart, descendant and stale-result tests.
  Reuse the existing repository and budget authorities; see `PROCESS_SUPERVISION.md`.
- [x] Add durable per-action intent/permit/result receipts through the parent
  repository, stable approval identities, bounded owner inspection and immutable
  unverified evidence. See `ACTION_RECEIPTS.md`; no automatic replay/refund.
- [ ] Define trusted provider/broker or separately authorized operator evidence
  verification and transactional recovery resolution. Owner attestations cannot
  grant success/retry/refund/recovery-clear or release authority. Local process
  termination does not establish remote outcomes.
- [ ] Verify Linux descendant containment and whole-service supervision before
  any production readiness claim. Windows tests are not Linux deployment proof.
- [x] Audit actual filesystem persistence and TypeScript generation-bound
  publication/recovery invariants; add bounded workspace checkpoint metadata,
  immutable local test objects and quarantined restore with corruption, crash
  and expiry cases. See `WORKSPACE_CHECKPOINTS.md`; session JSON is not file backup.
- [ ] Supply verified private storage, authoritative quiesced provider export and
  fresh-sandbox adoption/registry fencing, storage accounting and approved content/
  retention/deletion policy. The local adapter is not production E2B restore.
- [x] Audit and implement a disabled GitHub broker foundation using existing sealed
  action approvals, Plan/entitlement/receipt authority, typed bounded named-branch
  PR proposals and token-free native REST boundary. See `GITHUB_WRITE_BROKER.md`.
- [ ] Supply verified fresh-auth/account/project/session and installation/repository
  policy, real scoped issuance/revocation, isolated supervised service integration,
  named-branch approval UI/policy and trusted reconciliation. Immutable exact-diff
  approval is unsupported by this PR contract. Keep production GitHub writes disabled.
- [ ] Validate the full path in dedicated isolated Firebase/E2B/OAuth/Stripe test
  environments after explicit setup authorization. Never silently use production
  credentials, users, billing, provider calls or database migration.
- [ ] Audit monitoring and redacted operational logs, unresolved reservations,
  cleanup failures and backlog alerts; rehearse backup recovery and rollback.
  Lock dependencies and review data migration before proposing a release.

Contract evidence and concrete incompatibilities are in
`LOCAL_BACKEND_REVIEW_2026-10-25.md`. Current budget limits are development
admission bounds, not a proven monetary cap. Emulator tests do not establish
managed Firestore load behavior or external side-effect guarantees.

## Phase two: ongoing autonomy, after the reliable core

Every item below starts with an audit. These are requested areas to inspect,
not claims that implementations are absent. Reuse current code and verified
contracts before adding anything. Keep tasks and persistent autonomous agents
as explicit product concepts; do not infer their lifecycle from one coding run.

| Audit/implementation item | Current evidence | Implementation / local / live status |
| --- | --- | --- |
| Persistent memory | Per-user memory and `remember` already exist | Audit lifecycle, privacy, controls and limits; live unverified |
| Schedules and triggers | Not established by this checkpoint | Audit first; no scheduler work started |
| Connectors | GitHub OAuth exists; write execution disabled | Audit broker/access/revocation before extending |
| Notifications | No capability conclusion from this checkpoint | Audit first; no real messages authorized |
| Auditable run/action history | Durable jobs and step traces exist | Audit immutable action/effect/transcript coverage and recovery |
| Tenant isolation | Ownership checks and some negative tests exist | Broader account/project/memory/connector audit pending |
| Retention and deletion | No complete policy or implementation audit here | Audit approved rules, export, deletion and processor boundaries |
| Backups and restore | Not fully established; core workspace restore tracked above | Audit existing backups and perform an isolated restore rehearsal |
| Admin controls | No capability conclusion from this checkpoint | Audit least privilege, approval, audit trails and reconciliation |

- [ ] Complete the phase-two audits and record evidence/decisions per area.
- [ ] Prioritize one testable autonomy slice using that evidence; do not add all
  features at once or invent missing policy, permissions or notification targets.
- [ ] Mark each implemented slice, local checks and live verification separately.

## User-confirmed future connections/deployment backlog (no current implementation)

Users connect THEIR own Vercel/Netlify/AWS accounts, select tenant-owned targets
and revoke access. Providers bill the customer directly; no platform reseller
account is implied. Future Connections UX and adapters must reuse shared auth,
receipts, budget and approval; preview/production remain separate with framework/
runtime compatibility checks. User direction: Vercel integration OAuth; Netlify
public integration OAuth rather than pasted PAT; AWS limited cross-account role,
externalID and temporary STS rather than root keys. Skills are reusable workflows,
never a permission self-grant. No connector setup, real authorization/grant or
deployment is authorized by this backlog clarification. Current task-authorization
scope and tests remain unchanged; no connector work started.

## Teams/Business proposal and required authority audit (backlog only)

User asked about Business/Teams plans. Organisation-owned shared projects and
connections, owner/admin/member/viewer RBAC with distinct production deployment,
billing and secret privileges, seats/pooled usage with per-member limits,
audit/offboarding and later SSO/provisioning/retention are proposed architecture.
Exact commercial values and role-permission matrix are NOT approved.

Before implementation, audit existing organisation/team entities and current
membership/authorization semantics; do not assume those entities are absent.
Verified organisation membership/authorization is a prerequisite for shared BYO
connections. Current task grants remain personal-owner/session/project scoped;
this clarification grants no implicit organisation access, sharing, migration or
inheritance. Pricing, pooled/per-member limits and permission policy require
explicit decisions later. No organisation creation, invitations, membership or
permission changes are authorized or performed by this backlog note.

## Audited dependency/acceptance checkpoint

- [x] Audit existing org/team/family/sharing/skills/plan/connector source at exact
  revisions; distinguish active behavior, dormant UI, seed config and missing code.
- [x] Audit old GitHubActions/immutable artifact/SSH/systemd/Caddy/promotion and
  recovery-compatible rollback; preserve source/artifacts and document local plan.
- [ ] Prepare bounded FastAPI CI/release-validation files after parent direction;
  pin deterministic dependencies, real emulator and Linux process checks, artifact
  integrity and fail-closed readiness/staging/production/rollback gates. No publish.
- [ ] Reconcile validated catalog/account/usage and billing synchronization using
  existing stores; close exact frontend route/schema gaps and later lifecycle UI.
- [ ] Define orgownership/resourceadoption/role/offboarding and seat/pool/meter
  policy before shared project/connection/billing implementation; personal grants
  stay personal, family links are not org membership.
- [ ] Measure latency/reconnect/backpressure/memory/cancel/revocation at explicit
  concurrency targets; qualify Linux/index/config/readiness/observability/backup/
  rollback and separately authorized isolated live E2E before deployment claims.

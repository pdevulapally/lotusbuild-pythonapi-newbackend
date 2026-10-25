# Teams, Business, skills, BYO connections and delivery: source audit

2026-10-25. Read-only architecture audit; only this document, backlog and handoff
metadata change. No runtime/test/frontend/workflow/configuration edit, account/DB
read, secret inspection, invite, connector authorization, deployment or publication.
The prior686offline/80emulator checkpoint and its patches are preserved separately.

## Verified sources and meaning of evidence

Public master heads were reverified through read-only GitHub repository fetch:
frontend `8173d7c27f6bbb3c9fe040782d1a891219cffc45`, TypeScript server
`89a1bcac115e26a07c8990a3f491e84ee278b2ff`. Both recursive trees were complete.
Three independent readers inspected all172frontend runtime TS/TSX files,
all78server src/*.ts files and relevant Python services/configuration/docs.
The local Python tree is the tested isolated cumulative implementation, HEAD6110155
plus reviewed uncommitted changes; it is not deployed source.

The server's docs/recovered/README.md explicitly calls its recovered Postgres SQL
a specification, not an implementation, with21tables including migration metadata.
It preserves personal user/project/subscription/meter rules, not an organisation
membership implementation. No recovered backups/user rows were inspected. Seed
files and public schemas establish source semantics, not current production data
or approved commercial prices. Source paths below are exact within these revisions.

## Implemented versus missing

| Area | Verified existing behavior | Concrete gap / reuse boundary |
| --- | --- | --- |
| Organisation/team | Frontend retains workspace/owner/editor types, but AuthContext returns empty workspaces/null selection and switchWorkspace throws; team project queries reject. Python and TS ownership/billing use individual uid/ownerUid. No org/member/invite/RBAC/seat authority found in audited runtime code. | True shared Teams needs membership and organisation-owned resource authority plus policy; a plan label cannot grant it. Preserve personal projects/grants until explicit adoption/migration semantics exist. |
| Family and sharing | TS familyLinks supports confirmed parent-child linking and per-project parent-approved builds on the parent's entitlement; it does not delegate the parent's login or billing-management authority to the child. TS private/link/public project sharing grants read visibility. | These are existing separate access models, not editable team membership. Do not relabel family or public links as an org. readShared's blanket404 comment also overstates deletion behavior: status deleting can409 before visibility/token denial. Preserve opaque owner checks rather than copy that claim. |
| Business/Teams plans | TS seeds free/pro/business; Python seed includes free/pro/bloom/teams/business. Frontend pricing renders actual backend plans; dormant team UI compares singular team. Both billing stores attach subscriptions/customers to UID. | Catalog names, capability/meter schema and migration mapping diverge. Additional PERSONAL tier can be existing plan data after validation; seats, pools and shared billing are missing code/policy. Neither seed authorizes live values. |
| Dynamic configuration | TS loads/validates config/registry and plans together, cross-references, default-plan presence/pricing, typed capabilities/meters/prices/rate/network settings. Python reads raw plans and validates selected model/capability/build/task consumers later. | Complete validated catalog/account projection is missing code, alongside missing ENV/runtime configuration. Reuse existing entitlement/BudgetLedger authority; don't add a parallel commercial store or fabricate models/effort ceilings. |
| Billing | Both have UID Stripe customer/subscription paths. TS has durable checkout records, webhook event claims and fenced sync against current Stripe state. Python applies incoming subscription snapshots directly. | Port current-state reconciliation/idempotency through existing subscriptions, not a second billing ledger. Runtime plan/customer/price data were not inspected; required local environment remains unset and real Stripe configuration unverified. |
| Skills | TS has real owner-scoped CRUD/enabled catalogue, @name or /name loading, lazy use_skill and usage events. Prompt explicitly forbids skipping safety/exceeding tools. Frontend has no customer skills API/UI; Python has memory/remember, no skill service/runtime integration. | Reuse instruction/model/loading semantics; use current Python auth/receipts/Plan/task/tool restrictions. Do not copy TS fallback20 on any skills.max error, nontransactional rename/update or hard-delete as new policy. Skill version/retention/sharing still needs decisions. Repo .claude skills are developer instructions, not customer capability. |
| BYO connections/deploy | Python has browser-bound GitHub OAuth and encrypted UID token storage; its scoped write broker remains disabled. TS GitHubApp class has no runtime importer/binding found. Frontend Supabase views are callback-only and unmounted; active computer integration is project name only, with empty integration/deploy slots. | Generic customer connection catalogue/target binding/revoke and Vercel/Netlify/AWS deployment adapters are missing code, not merely ENV. GitHub sign-in is authentication, not repo linking. Provider account/installation IDs are not LotusBuild org membership. No active deploy API in current client. |

Supporting source references:

- Frontend contexts/auth-context.tsx10–26,37–87; hooks/use-project-list.ts10–27;
  lib/projects/types.ts61–99; components/projects/projects-workspace.tsx538–595;
  components/settings/settings-dialog.tsx14–35,311–353;
  app/computer/[id]/page.tsx283,475–476; app/pricing/page.tsx46–63,124–159.
- TS src/family/service.ts11,85–102 and docs/family-access.md;
  src/projects/repository.ts189–240,404–435; src/users/repository.ts120–147;
  src/github/app.ts (class only); test/project-sharing.test.ts.
- TS src/entitlements/store.ts19–84, schema.ts192–213,519–590;
  src/seed/config.ts164,201,236; src/billing/repository.ts83–175,
  src/billing/routes.ts348–366. Recovered docs/recovered/postgres-schema.sql258–583
  and postgres-seed-config.sql127–132 are reference-only.
- TS src/skills/model.ts3–29, prompt.ts13–18, repository.ts45–51,92–105,
  routes.ts; src/runner/loop.ts170–181 and agent.ts1425–1453.
- Python app/execution/contracts.py137–147; app/billing/plans.py17–22,DEFAULT_PLANS;
  app/billing/entitlements.py8–50; app/billing/routes.py67–86;
  app/github/{routes,oauth,credentials,broker}.py; app/agent/{loop,tools}.py.

## Exact current frontend usability blockers

Frontend account bootstrap needs /me then /me/usage; failures clear account state
and prevent build submission (contexts/auth-context.tsx37–70). Python intentionally
503s both (app/api/contracts.py63–73). Public /plans is absent. Checkout sends
{planKey} without an Idempotency-Key, whereas Python expects {tier_id} plus a key
(lib/backend/client.ts170–174 versus app/billing/routes.py28–58).

Client sessions require uppercase four-state status, projectId/model/effort;
default project-session create/list/cancel remain blocked, session GET is native,
and client message {prompt,...} conflicts with native {content,mode?}. Files/
changes/archive intentionally501; client attachments, desktop tickets, usage and
email-verification routes are absent. Native events do not supply every client
text/tool/preview/save signal. Native queue expiry/consent routes are not integrated
in the client. These are code/schema gaps; setting ENV cannot close them. Exact
older matrices remain in ACCOUNT_PROJECT_HISTORY_CONTRACT.md and queue contract.

The UX decision is already made: add explicit approval/cancellation/recovery
states later. Preserve native states now; full compatibility with an unchanged
four-state client is not demonstrable. No frontend edits are part of this audit.

## User direction and small decisions before shared authority

Confirmed: customers connect THEIR accounts and pay providers directly. Use
Vercel integration OAuth, Netlify public integration OAuth rather than pasted PAT,
and limited AWS cross-account role+externalID+temporary STS rather than root keys.
Future adapters reuse shared auth/receipts/budget/approval, target exact customer
project/environment, distinguish preview/production and check runtime/framework
compatibility. This direction grants no credentials or deployment permission.

Only these decisions should precede the affected implementation:

1. Commercial authority/mapping: which existing catalog keys/capabilities/meters
   are the target product contract, and how do old tiers/usage map without free
   resets? Decide actual seat/pool/per-member charging values separately; no
   runtime price/allowance was read or approved here.
2. Shared ownership and permission matrix: personal versus org project creation/
   adoption; who may invite/offboard, connect/revoke provider accounts, read
   secrets, manage billing and approve production/chargeable actions? Define
   offboarding effects on owned projects, connections and already queued work.
   Owner/admin/member/viewer is a proposal, not an approved permission table.
3. Skill scope/version/retention: personal first or org-shared, effect of edits
   on already admitted work, and recoverable removal. Workflow text never grants
   tools or connected-service permission. Current task grants remain personal.

Operational latency/concurrency/recovery targets are additional measurable
acceptance inputs, not commercial defaults. No invites/org creation/migration or
policy decision is silently performed by documenting these questions.

## Verified old CI/CD and concrete next pipeline plan

The old pipeline is .github/workflows/ci.yml, not an assumed Jenkins/container
deployment. PR/push/manual jobs run on Ubuntu with Node22, pinned action SHAs,
read-only repository permission and checkout credentials disabled. Check does
npm ci/typecheck/build/test, Python release-validator tests and a secret scan.
It packages production dependencies plus dist with commit/recovery metadata,
checks an immutable archive/digest and uploads by exact artifact ID. There is no
Firestore emulator job or staging promotion in the inspected workflow.

Separate production job requires check outputs and the production environment,
serializes noncancellable deploy, downloads the exact artifact, and uses SSH with
a pinned host key. deploy.sh verifies digest/checkout revision, validates archive,
locks promotion, preflights candidate, confirms both services stopped, atomically
exchanges directories, starts API/runner, checks health and attempts only
recovery-protocol-compatible rollback. Old setup is Caddy TLS -> loopback API plus
separate systemd API/runner; Dockerfile is an alternative build artifact, not
what this workflow deploys. No old host, keys, services or secrets were contacted.

Next bounded LOCAL pipeline preparation, after parent direction:

- Add .github/workflows/ci.yml for FastAPI checks, a reviewed locked dependency
  input and release metadata/validation scripts under scripts/ or deploy/. Pin
  verified runner/Python/dependency/action inputs; no mutable broad install used
  as reproducibility evidence. Current requirements are ranges, not a lock.
- Run static/dependency/secret checks, socket-blocked synthetic offline suite,
  real loopback Firestore SDK/emulator tests with explicit no-ADC demo settings,
  and publish JUnit/check evidence. Preserve existing Windows containment tests
  while adding REQUIRED Linux execution/process/restart/containment validation;
  a skip on Windows-only safety tests cannot certify Linux release readiness.
- Build one immutable Linux-compatible Python release/artifact with dependency,
  commit and recovery-schema manifest/digest; validate archive paths/links and
  compare the exact tested artifact on a clean runner. Build/tests run without
  production secrets; never package local credentials or a Windows venv.
- Prepare fail-closed staging/preflight/health/smoke and promotion/rollback gates
  using the old exact-artifact/serialized API+worker/recovery-compatibility
  principles. Missing readiness policy, config, credential wiring or safe worker
  platform proof must block release; retain experimental worker/broker guards.
- Keep CD publication/enabling, environments/reviewers, SSH/OIDC/secrets/hosts and
  all server operations unconfigured and unauthorized in that local slice.
  A configuration-only disabled placeholder is not a successful deployment.

This audit does not create those pipeline files. The user requested CI/CD;
production rollout remains gated after local pipeline/source verification.

## Dependency order and acceptance for a usable product

1. Prepare the bounded local CI/release-validation stage above for the preserved
   implementation, measuring Linux gaps honestly. In product work, reconcile
   validated plans/catalog, account/usage projection and billing synchronization
   through existing authorities before adding a second commercial/tenant model.
2. Demonstrate the exact frontend read/create/message/event/workspace path with
   synthetic HTTP/SSE contract fixtures, then separately authorized isolated real
   integration. Coordinate the already-chosen explicit lifecycle UI update later.
   Honest errors remain for unsupported surfaces; do not manufacture empty data.
3. Personal skills can reuse existing TS semantics under native safety. Shared
   projects/connections/pools need the explicit org decisions and membership/
   resource-ownership foundation first. Keep provider account identity, user role
   and org ownership distinct. Do not migrate personal task grants implicitly.
4. Add individually authorized BYO adapters against that authority, then qualify
   staging/production delivery and recovery; no current connector/deploy work.

| Target acceptance | Existing evidence | Remaining concrete gate |
| --- | --- | --- |
| Dynamic | Typed native selection/Plan/build/task bounds; TS validated registry model available | Reconcile validated schema and catalog/account/billing projection; missing capabilities fail closed; no fabricated tier/model/effort values or arbitrary runtime code. |
| Smooth/fast | Resumable events, bounded I/O/auth/socket admission; frontend300ms initial retry, bounded backoff/cursor,150ms refresh coalescing and animation-frame text updates | Measure command/queue acceptance, first event, cancellation/revocation delay, reconnect replay, slow-reader memory and cleanup at agreed concurrency; real browser/proxy continuity and cluster capacity. No speed/SLA claim from test count. |
| Frontend compatible | Current8173source verified; partial HTTP/history/event contracts and exact blocked-route matrices | Exact pinned client fixture/browser/E2E evidence, stable selections/history/receipts and honest native lifecycle presentation. /me,/me/usage,/plans, checkout/session/workspace gaps above remain. |
| Deployable | Local686/80 safety evidence; old source release validator/atomic promotion pattern | Dependency lock, reviewed config/index/migration inventory, real readiness/liveness/worker checks, Linux whole-service containment, secrets setup separately authorized, bounded external calls, monitoring/backup/rollback rehearsal and isolated live integration. |

Python /health is static ok (app/main.py121–123), not dependency readiness. SSE
poll/I/O defaults and per-process admission limits (api/streaming.py22–105) are
bounds, not latency/capacity measurements; timed-out thread waits cannot kill SDK
calls. See SESSION_EVENTS_CONTRACT.md67–78, PROCESS_SUPERVISION.md70–87,
FIRESTORE_EMULATOR_TESTS.md88–91,131. Emulator does not validate/deploy production
indexes. These gates remain even when CI is green. No application tests were
rerun for this documentation-only audit; source hashes and the prior successful
686/80 evidence are preserved in the separate audit manifest.

# Configured model selection and enforced Plan capabilities

Bounded local backend slice, 2026-10-25. No live discovery, provider execution,
E2B, billing, production database, migration or frontend change was performed.

## Verified source and remaining configuration dependency

Inspected frontend `8173d7c27f6bbb3c9fe040782d1a891219cffc45` and server
`89a1bcac115e26a07c8990a3f491e84ee278b2ff` through read-only source tools:

- [Frontend selection](https://github.com/pdevulapally/Lotus-Build/blob/8173d7c27f6bbb3c9fe040782d1a891219cffc45/lib/backend/model-selection.ts):
  only an unset choice gets a default; explicit choices never silently change.
- [Provider catalog](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/models/catalog.ts):
  model IDs, effort support and output ceilings come from validated dynamic
  Anthropic metadata. The frontend effort names are low/standard/high/xhigh;
  standard maps to provider medium. That mapping alone grants no capability.
- [Effort admission](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/entitlements/build-admission.ts):
  entitlement capability ceilings constrain supported effort and its default.
- [Session routes](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/sessions/routes.ts):
  model/effort fixed at creation, idle message may change mode.
- [Read-only tools](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/runner/tools.ts):
  read_file/list_files/search_files; arbitrary commands excluded.

Python's existing authoritative plan records contain provider/model/quota and
request/token limits. They do **not** supply verified model effort capabilities,
paid effort ceilings, or the current server's catalog cache contract. Seed model
IDs are not provider capability evidence. Local environment variables are unset.
This slice does not invent capability metadata, catalog IDs or paid allowances.

Consequently all explicit efforts fail before command/job/slot/provider work,
with 503 `session/model-unavailable` and a fixed configuration message. Invalid
effort names/null/types reject as 422. There is no accepted effort state and no
provider effort argument is silently omitted for an accepted request. Existing
native requests without effort retain their existing provider defaults. Full
frontend effort/catalog compatibility remains blocked by actual capability and
entitlement configuration plus a separately reviewed discovery/cache integration.
No live catalog retrieval was attempted or claimed; synthetic model fixtures
exercise the existing plan resolver and adapter boundary only.

## One selection and spend authority

Native `POST /sessions` accepts optional `model` and `mode` (build/plan). Model
means an exact ID from the current user's configured plan. Duplicate provider
matches reject as ambiguous. Unsupported providers/desktop capability reject
without constructing a client or provisioning a sandbox. Explicit model choices
never use the legacy quota fallback; exhaustion stops through BudgetLedger.

`billing.model_select.resolve_model` is the exact selection authority for HTTP
admission, worker selection, approval and shared `metered_create`. Current quota
comes from current entitlements, never from saved approval quota. Subscription
and plan reads participate in parent command/start/permit transactions. Compaction
uses the same chosen model and ordinary metering; no alternate paid call appears.

Session provider/model are persisted and immutable to worker saves. New messages
cannot change them. `MessageIn.mode` changes mode only with an idle next turn;
busy/approval/recovery transitions remain native and explicit. Every approval,
including a historical session without top-level selection fields, rechecks its
exact pending model before the parent grants a tool permit. New worker processes
and recovery retain these fields. Unknown/corrupt persisted selections fail closed.

Absent model fields keep the historical native fallback path. Historical HTTP
create fingerprints retain the kind=code default so lost-response retries replay
their old durable receipts. New optional fields remain part of idempotent input.
No new paid tier values, plan capabilities, catalog endpoint, project API or
frontend state projection was added.

## Enforced Plan mode

`agent.policy` is the shared capability gate. Each registered tool has an
explicit Plan capability, checked against both its canonical name and the
verified read/list/search set. Current Python exposes only read_file from that
set. No fake list/search implementation or shell substitute is offered.

Provider schemas are filtered through that same policy. `_invoke` checks it
before direct handlers. Parent `prepare_actions` checks the complete batch before
writing any receipt; `permit_action` checks the current persisted mode before
granting each one-time permit. A forged child Build snapshot, copied alias,
unknown wrapper/batch, arbitrary shell command or approved historical mutation
cannot bypass it. Worker saves cannot change mode. Missing historical mode means
Build; present null/unknown mode cannot default to Build.

All virtual mutations, remember/update_plan, screenshot, GUI actions, shell-backed
git_diff, command execution and disabled GitHub writes remain unavailable in
Plan. Session journals, transcripts, model accounting and infrastructure lifecycle
continue through their existing authorities. This is a workspace/tool capability
restriction, not a claim that those required persistence operations stop writing.

## Verification and limits

Final checks:357 offline passed/38 emulator skipped, including41 actual Windows
process cases; separate38 Firestore emulator cases passed/zero skips. Three
pre-existing memory datetime warnings remain. See task-4 `selection-final-*`
evidence and checkpoint manifest for hashes. Three independent read-only reviewers checked contract, security
and regression/maintainability. Their historical-idempotency and legacy-approval
findings became regressions and were confirmed fixed. Only the integration owner
edited. Focused checkpoint: 166 passed, including actual spawned child forgery.

Tests cover HTTP malformed/unsupported selections, missing configuration, exact
provider choice, ambiguous IDs, quota exhaustion without fallback, compaction,
subscription/current-entitlement boundaries, immutable selections, mode changes,
approval/restart/recovery, mixed batches, aliases, direct dispatch and parent RPC.
Real SDK/emulator cases use anonymous credentials and a loopback demo project;
process cases use local markers only. None validates real provider models.

Existing limits remain: experimental worker guard, disabled GitHub write tool,
trusted remote outcome resolution missing, Linux descendant containment unverified,
parent DB/finalization blocking and operation-boundary deadline limits. Receipts
do not establish remote exactly-once or cancellation. Do not release, refund,
replay or clear uncertain recovery based on this selection/policy slice.

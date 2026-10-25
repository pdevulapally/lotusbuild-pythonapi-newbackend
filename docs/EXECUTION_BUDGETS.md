# Experimental execution admission and usage reservations

**Local review foundation, not a production billing or dollar-spend guarantee.**
`ENABLE_EXPERIMENTAL_WORKER=1` remains required. No real Firestore, E2B, model,
Stripe, or paid-provider integration has been validated by the offline tests.

## Admission invariants

- The same Firestore transaction commits command receipt, queued job/session,
  active-run slot, and (on create) sandbox slot. Replaying an idempotency key
  consumes no second slot. Rejection is HTTP 429 without partial documents.
- Deployment-wide **development** defaults are 2 active/queued runs and 3
  reserved sandboxes **per authenticated UID**. Configure with
  `MAX_ACTIVE_RUNS_PER_USER` / `MAX_SANDBOXES_PER_USER` (integers 1..100).
  All API/worker instances must have identical settings. These are not paid
  plan promises, organization seats, or globally bounded platform capacity.
- Slots are held before workers can call a provisioner. `start` refuses jobs
  without their reserved slots, including pre-upgrade jobs. Idle and approval
  pause release run capacity, but keep sandbox capacity for the live session.
- Certain cooperative cancellation releases the run; only confirmed cleanup
  releases its sandbox. Provider cleanup errors retain the slot. Failed,
  interrupted, deadline-expired, or otherwise ambiguous execution retains
  capacity without automatic TTL reclamation. Even cancelling an interrupted
  session must not release its uncertain run slot.
- This intentionally sacrifices availability: two uncertain runs can block a
  user's new work until manual reconciliation. Operators must first fence/stop
  the old worker, establish remote resource/effect outcomes, and reconcile
  session, cleanup, and slot records transactionally. No unsafe reset endpoint
  or automatic time-based refund is provided.

## Model-call accounting

Every ordinary model iteration **and compaction** uses `metered_create`:

1. Check durable worker authority; resolve current entitlement and find the
   requested model in it (do not trust a saved approval's old quota).
2. Bound output to at most 4,096 tokens and rendered text request envelope to
   96,000 UTF-8 bytes. Reserve `2 * envelope_bytes + 4096 + output_cap` tokens.
3. Atomically reserve one daily request and the full monthly token allowance
   against BOTH the user's tier and optional model limits. Two simultaneous
   callers cannot reserve the same remaining allowance. Day/month and model
   identities and the durable run ID are stored in a unique reservation receipt
   before provider I/O, allowing operators to correlate uncertain calls.
4. Check worker authority again; invoke provider once. SDK implicit retries
   are disabled, with a 60-second HTTP timeout setting.
5. Settle known nonnegative integer usage once against the original periods,
   including Anthropic cached input tokens. Requests are not refunded. Missing
   usage, exceptions, timeouts, or a crashed worker retain the full reservation.
   Observed usage above the allowance records the overrun and blocks further
   reservations on those period ledgers pending reconciliation.

Settlement is accounting only: it may happen after cancellation, but cannot
restart tools or publish an agent result. Uncertain reservations are never
reused to retry a provider call. A database failure before reservation commits
prevents the provider call; failure after a provider call retains capacity.

**Important limits:** the byte-envelope formula is a conservative development
allowance, not a verified tokenizer upper bound for all provider/model wire
formats. Observed usage can exceed it; stopping afterward does not undo spend.
There is no pricing table, money-denominated budget, organization-wide cap,
provider invoice reconciliation, or exactly-once billing guarantee. Entitlement
reads occur immediately before reservation but are not a transaction with
subscription changes; an already-authorized in-flight call is not revoked.
Provider timeout settings are not hard wall-clock process termination. E2B
create/connect requests use 30-second request timeouts and zero SDK retries;
configured sandbox lifetimes are limited to 1..900 seconds. Reconnecting can
extend lifetime: this is NOT a monthly sandbox compute-spend budget.

## Compatibility changes and migration blockers

- Native provider web search is disabled because server-tool calls and their
  extra charges are not covered by this ledger. Multimedia model inputs fail
  closed. Desktop screenshot/control endpoints are not removed, but visual
  autonomous reasoning is unavailable until verified image metering exists.
- Compaction uses the current turn's authorized model and reserves independently.
  Its default heuristic threshold is lowered to 8,000 estimated tokens to begin
  before the envelope cap for typical text. Large recent messages, large tools,
  or large user memory can still exceed the hard envelope cap and stop the run.
  The former `COMPACTION_PROVIDER` and `COMPACTION_MODEL` settings are no longer
  used. `COMPACTION_TOKEN_THRESHOLD` and `COMPACTION_KEEP_LAST_N` still apply.
- Advisory `pick_model` reads the v2 ledger; it is not admission authority. A
  model with some remaining quota but insufficient allowance for the entire
  request fails before calling the provider rather than silently switching
  providers halfway through an approval-bound turn.
- New collections: `agent_v2_slots`, `agent_v2_budgets`, and
  `agent_v2_model_reservations`. Budget doc keys hash UID+period; model keys hash
  provider+model without the slash-to-underscore collision of legacy counters.
  Legacy `usage/.../counters` are NOT automatically copied or dual-written.
  Do not deploy this against existing paid customers or alongside old workers
  until a separately reviewed migration imports existing period usage and
  inventories all existing jobs/sandboxes into slots. New empty ledgers would
  otherwise reset their allowance. Foundation jobs without slots do not run.
- No automatic TTL on reservations or slot records. Retention, reconciliation
  tooling, Firestore document growth under catalog churn, and production
  contention/load tests remain release gates.

## Offline verification

`python -m pytest -q` uses a lock-serialized fake transaction store with all-
reads-before-writes enforcement. Tests cover concurrent admission and allowance
races, atomic rollback, exactly-once local settlement across month boundaries,
invalid usage, overrun fencing, entitlement changes, provider exceptions,
cancellation, uncertain cleanup, timeout configuration, and existing regression
coverage. This is NOT a substitute for Firestore emulator retry/contention tests
or authorized provider integrations.

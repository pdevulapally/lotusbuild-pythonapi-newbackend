# Durable action receipts and reconciliation evidence

Local bounded slice, 2026-10-25. Foundation `6110155` and all earlier uncommitted
budget/security/event/safety/process changes are preserved. This is experimental
backend reliability groundwork, not a deployed autonomous product.

## Single authority and identity

`RunRepository` owns preparation, permission and result transitions through
`_authority_tx`; existing claim/fence, lease, deadline and cancellation rules
remain authoritative. `Guard` and the existing strictly sequenced bounded JSON
process RPC carry narrow action requests. No second runner, budget ledger, auth
policy or provider client was introduced.

Before dispatch or an approval pause, the loop seals a bounded ordered batch.
Batch identity hashes session + originating run + model-step ordinal. Action
identity hashes that batch + call ordinal. Duplicate call IDs and changed sealed
input/order reject; separate intentional identical calls remain separate actions.
The seal stores tool names, input digests, effect/risk classifications and opaque
call IDs, without raw arguments or output. Validation and risk classification
reuse the existing tool registry/schema and risk policy.

The session action sequence and lifecycle update commit atomically with batch
and prepared action records. Ordinary child saves cannot reset this head or
replace sealed pending approval calls. Approval submission copies the original
batch into the new job and stores a separate immutable `approval_batch_id`.
Later model batches in that job cannot borrow an old decision by reusing its
call ID. Existing model entitlement/budget checks remain unchanged.

## Receipt meaning and failure windows

| State | Runtime evidence | Allowed next behavior |
| --- | --- | --- |
| `prepared` | Intent committed; this action has never received a dispatch permit | One fenced permit may be issued under current run/approval authority |
| `outcome_unknown` | Permit committed; result receipt absent | Stop and inspect; never issue another permit, replay, refund or release uncertain capacity |
| `handler_returned` | Handler returned bounded JSON and its digest was recorded by the fenced runtime | Continue the current run; this does not verify the intended external effect |

The permit stores run/worker identity, fence, issuance time, lease-expiry and run
deadline snapshots. Permission issuance is a distinct transaction after intent.
A lost permission reply is unknown even if the handler never started. Duplicate
delivery never returns a reusable permission. A known result is idempotent only
for the exact same digest under valid authority; stale/expired results reject.

The shared handler boundary requires a durable claim even for a direct internal
call. All permitted calls, including observations, receive receipts. A handler
exception, unusable/oversized result, receipt checkpoint error or lost storage
reply stops execution before another tool/model call. An open action also blocks
another permit, another batch and terminal capacity release in the repository.
Known pre-permit validation/capability rejection and denied approvals can leave
a prepared record; no permission was issued. Prepared records are not effects.

A crash after an effect and before its receipt remains unknown. A crash after a
known result can still leave the overall run recovery-required. Existing run,
sandbox and model reservations remain conservative and independent. No automatic
refund, TTL reclamation, reset, replay or retry endpoint was added.

## Owner inspection and reconciliation evidence

The ordinary Firebase UID and existing mutation rate limit protect:

- `GET /sessions/{id}/actions?after={sequence}&limit=50`
- `GET /sessions/{id}/actions/{action_id}?after={evidence_sequence}&limit=50`
- `POST /sessions/{id}/actions/{action_id}/reconciliation-evidence`

Pages contain at most 100 records and require contiguous observed history.
Ownership checks precede receipt replay and action/cursor interpretation in the
repository. Cross-session and cross-tenant action IDs reject. Public allowlists
exclude raw tool arguments/output, provider call IDs, worker identity, exception
text and private evidence fields. Missing history fails closed. Empty historical
ledgers do not prove that an older runtime performed no external effects.

POST requires the existing `Idempotency-Key` and a strict body:

```json
{
  "expected_version": 2,
  "assertion": "inconclusive",
  "evidence_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
}
```

Assertions are `effect_observed`, `no_effect_observed` or `inconclusive`. The hash
is supplied by the owner; no URL, raw evidence, free-text diagnostic or secret is
accepted or fetched. The transaction creates an immutable audit record, advances
the action version/evidence sequence and stores a durable command response.
Concurrent conflicting versions reject with 409. Same-key exact replay returns
the original response without a second record. This path never creates a job.

Every record explicitly says `unverified_owner_attestation`. The existing bearer
proves UID ownership, not that the caller is human or an operator. An assertion
or digest cannot verify a remote effect. Recording evidence does not change the
execution state, clear recovery, mark success, dispatch work, settle budgets or
release slots. Trusted provider/broker evidence or a separately authorized
operator-verification contract remains required for actual recovery resolution.

## Verification and limits

Final local verification: **309 offline passed, 35 emulator skipped**, followed
by **35 separate real-SDK/demo-emulator cases passed, no skips**. The offline
suite includes 30 new receipt cases and six new real spawned process cases,
alongside the original 273 tests (33 actual process cases). Three existing memory
datetime warnings remain. Compileall, pip check and git diff --check pass.

Coverage includes duplicate delivery, changed input/order, stale fences/results,
lost commit replies, failing pre-result checkpoint, crash before permit/after
permit/after local effect/after receipt, restart, terminal gates, same-batch open
permits, old approval decision reuse, successful normal/approval dispatch identity,
version races, owner-before-replay checks, rollback, bounded secret-safe projection
and strict HTTP bodies. Process effects write task-owned local marker files only.
Emulator teardown removes only each synthetic UID's records.

Three independent read-only reviewers checked security, concurrency and
maintainability. Their blockers were fixed and regression-tested. No live model,
E2B, OAuth, billing, production Firebase, signed-in browser or Linux process
verification occurred. Emulator contention can reject within bounded SDK retries;
this is availability evidence, not permission to repeat external effects.

New collections: `agent_v2_action_batches`, `agent_v2_actions`,
`agent_v2_action_evidence`; existing `agent_v2_commands` stores evidence command
receipts. Composite action/evidence indexes are declared, not deployed. Existing
sessions are not backfilled. Retention/export/deletion, migration, operational
monitoring and trusted recovery resolution remain release dependencies.

Receipts cover model-dispatched tools; sandbox provisioning/cleanup and model
usage retain their existing separate authorities. The runtime remains trusted;
this is not a hostile-code or credential-isolation boundary. There is no external
exactly-once guarantee without provider support. Local process kill does not prove
remote cancellation. Linux descendant containment and parent DB/finalization
delays remain as documented in PROCESS_SUPERVISION.md. Keep the experimental
worker guard and disabled GitHub write tool.

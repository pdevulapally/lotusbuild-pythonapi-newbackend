# Local Firestore transaction verification

## Result on 2026-10-25

The exact reviewed budget patch (57,560 bytes, SHA256
`808e3749daa2ed31f7fc77610c19aac384d4eb2c78d8282daabd1e076825c46d`)
applied cleanly to foundation `6110155` plus the independent diagnostics fix.
The combined offline suite passed **151 tests**. The separate integration suite
passed **11 tests, no skips**, using the real Firestore Python SDK and cached
Firestore emulator v1.21.0 with Java 21.0.11 on Windows.

The final six-way budget race admitted 3 reservations, rejected 3 for quota, and
left exactly the corresponding receipts/counters. Earlier runs also observed
transaction lock timeouts: the SDK can exhaust its five commit attempts, and
an ABORTED response during a transaction read can propagate directly. The tests
check that such contention failures create no partial reservation or extra
allowance; they do not require all affordable calls to succeed under contention.
Production contention behavior and friendly handling of temporary database
admission failures remain operational follow-up work.

The deterministic retry test injects one known abort before commit dispatch,
first rolling back the actual emulator transaction to model released server
locks. The production SDK decorator then starts a new transaction and commits
one reservation. The first fault-injection attempt did not release those locks;
that test harness was corrected, without changing production retry behavior.

## Scope and safety

- `tests/integration/test_firestore_execution.py` refuses a nonnumeric or
  nonloopback emulator endpoint. Without `FIRESTORE_EMULATOR_HOST`, tests skip.
  An explicitly configured but unavailable emulator fails rather than falling
  back to any remote database.
- The fixed project ID is `demo-lotusbuild-local`. Clients use
  `AnonymousCredentials` and the emulator channel; no Firebase initialization,
  ADC lookup or service-account data is needed.
- Each test creates a unique synthetic UID. Teardown deletes only documents
  belonging to that UID and its cleanup records. It never clears the project.
- `RunRepository` and `BudgetLedger` use their production transaction wrappers;
  no MemoryDB or injected transaction runner is used in these tests.
- Model providers, E2B, OAuth and Stripe are not invoked by the integration suite.
  Cleanup effects are replaced by test callbacks while state commits use the
  real emulator. The worker was not enabled.

Covered behavior:

1. Same-key command races commit one receipt, session, job and admission slot.
2. Competing submissions enforce capacity and leave no rejected-command residue.
3. Competing workers, lease reclaim, stale fences and heartbeat ownership.
4. Competing approval keys claim one immutable batch; foreign-owner cancel fails.
5. Cancel/save races, cleanup retries and confirmed capacity release.
6. Expired running jobs stop without replay or uncertain-capacity refunds.
7. Heartbeat deadlines revoke authority and retain uncertain capacity.
8. Budget races maintain consistent counters/receipts within allowance.
9. Concurrent settlement is idempotent across the original month boundary.
10. An injected known-aborted commit retries without duplicate reservation.
11. Observed provider usage overruns settle once and block later admission.

## Reproduce locally

Use the task virtual environment with `requirements.txt` and
`requirements-dev.txt`. Start the cached emulator in a separate local terminal:

```powershell
java -jar 'C:\Users\Preet\.cache\firebase\emulators\cloud-firestore-emulator-v1.21.0.jar' --host=127.0.0.1 --port=8787 --project_id=demo-lotusbuild-local --single_project_mode=true --single_project_mode_error=true
```

From the isolated backend checkout:

```powershell
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8787'
& '..\.venv\Scripts\python.exe' -m pytest -q tests/integration/test_firestore_execution.py --tb=short -o junit_family=legacy --junitxml=../verification/firestore-integration.junit.xml
```

Stop the emulator after verification. It is not an application service and has
no connection to a live Firebase project. The actual task launch was hidden,
bound to loopback, with stdout/stderr saved under `..\verification`.

Evidence in the task workspace:

- `verification/combined-budget-diagnostics-pytest.txt`: 151 offline cases.
- `verification/firestore-integration-pytest.txt`: final 11 emulator cases.
- `verification/firestore-integration.junit.xml`: case results and budget-race counts.
- `verification/firestore-integration-initial-failures.txt`: initial investigation.
- `verification/installed-packages.txt`: installed dependency snapshot.

## Limits and maintainability review

Firebase documents that emulator transactions differ from production, locks can
take longer to clear, compound indexes are not checked, and some production
limits are not enforced. See the
[official emulator limitations](https://firebase.google.com/docs/emulator-suite/connect_firestore#how_the_cloud_firestore_emulator_differs_from_production).
These results are not managed-Firestore load, index, size-limit or disaster
recovery validation. They also do not verify real authentication, provider
timeouts/interruption, sandbox termination, OAuth, billing or end-to-end UI flow.

The combined code was reviewed for duplicate authority before expanding scope:
`BudgetLedger` owns usage admission/settlement; `metered_create` is the sole
ordinary-model/compaction invocation boundary; `usage.py` only reads advisory
ledger state. `get_uid` remains the shared authentication dependency, and
`RunRepository` owns command and run state transitions. API handlers delegate
to it. New integration helpers only arrange inputs/read results; they do not
reimplement admission or state transition rules. No second billing path, auth
dependency, job runner or compatibility API was added.

The worker guard remains unchanged. Later event and execution-safety slices
extend this suite to **21 passing emulator cases**. They add immutable event
replay/gap checks, deadline checkpoint/save/finish boundaries without heartbeat,
cancellation-grace commits, oversized-save rollback, uncertain tool/cancellation
finalization and synthetic worker exit without replay. See `EXECUTION_SAFETY.md`
and `verification/safety-firestore-integration.junit.xml` for the latest evidence.
The next increment is process supervision and explicit action reconciliation,
not deployment. Real provider effects and OS process kills remain untested.

The subsequent process slice extends the final emulator suite to **24 passing
cases**, including spawned child RPC saves, cancellation of a hanging child and
an actual killed worker parent followed by stale-lease recovery without replay
or budget refund. Current evidence is `supervision-firestore-integration*` and
`PROCESS_SUPERVISION.md`; earlier counts/untested-OS-kill wording above describe
the earlier checkpoints. The final emulator is stopped. Linux and real provider
effects/termination remain unverified.


## Action receipt checkpoint, 2026-10-25

The current suite passes 35 cases with no skips using the same loopback demo
emulator/anonymous credentials. Eleven added cases cover immutable batch/permit
races, idempotent/conflicting result receipts, precommit rollback of each receipt
transition and evidence, owner/version evidence conflicts, four real spawned
crash windows, old approval reuse and open-action gates. Teardown includes all
three new UID-scoped receipt/evidence collections. Declared new composite indexes
were not deployed; emulator success does not validate production indexes.
See `ACTION_RECEIPTS.md` and the latest `NEXT_TASK_HANDOFF.md` for evidence paths.

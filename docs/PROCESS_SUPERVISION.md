# Local execution process supervision

2026-10-25. Bounded backend slice after `EXECUTION_SAFETY.md`. The experimental
worker guard remains required; neither production operation nor live SDK calls
were enabled. Desktop original and frontend checkouts remain unchanged.

## Implementation and authority

`worker.process_claim` uses `execution.supervision.supervise` by default. A
fresh Windows-compatible `spawn` process runs the existing trusted handler.
There is no environment switch to an inline production fallback. Legacy unit
tests explicitly inject a test-only inline adapter so their closures and mocked
SDKs stay meaningful; the process tests use real spawned children.

The parent commits `RunRepository.start` before releasing runnable work. Only
the parent owns the immutable claim and repository transition calls. The child
uses the existing execution `Guard` through a checkpoint/save RPC proxy; no
Firestore client, repository implementation or arbitrary exception object is
pickled into the child. SDK imports remain lazy inside the existing handler.
BudgetLedger/metered_create remain the only model admission/settlement path;
process interruption does not refund reservations or replay the model call.

Runtime RPC uses capped JSON byte frames, exact operation shapes and sequential
request IDs. Pipe reading/writing use bounded queues on separate threads, so a
partial or blocked frame cannot occupy the termination watcher. Malformed,
oversized, deeply nested and out-of-sequence messages fail closed. Clean EOF is
an internal transport marker that child JSON cannot spoof. Typed recovery
messages accept only fixed reasons and registered tool names; raw exception
text and handler return values are not forwarded. Python child stdout/stderr
are discarded; this is not a general hostile-code logging sandbox.

Nonterminal saves delegate immediately to the existing repository and return
its authoritative revision/counters. Terminal idle/approval saves are staged.
Publication requires a completion frame, validated EOF, clean child exit,
reaping owned containment, then fresh repository authorization/save/finish.
No later action checkpoint or save is accepted after staging. A terminal-save
then hang/crash/invalid frame cannot release run capacity or allow another turn.
Identity/revision/fence rules stay in RunRepository. A rejected result attempts
the same fenced failed finalization; changed/expired owners still cannot write.

An independent local watcher uses monotonic remaining deadline/lease bounds
derived from the authoritative job timestamps and successful heartbeats. It
does no database or pipe I/O. Shutdown, loss of authority or a local bound stops
owned execution. A forced stop is uncertain even when its local process exits:
failed finalization retains run/sandbox capacity and durable cleanup intent.
No child or started job is automatically replayed. Unstarted leased work remains
safely reclaimable if shutdown was already requested before start.

`_OwnedChild` owns the single spawn, transport, watcher and reap implementation.
Both run handlers and remote sandbox cleanup use it. Cleanup has a 30-second
local execution bound; `RunRepository.cleanup_once` still owns cleanup claims,
settlement, retries and conservative uncertain-resource retention. Its SDK call
no longer blocks directly in the parent poller. Any unconfirmed physical stop
latches the shared stopping event at the first failed reap and preserves that
failure even if final cleanup retries succeed; the worker does not claim new work.

## OS behavior and limits

Windows assigns the newly created process handle to an unnamed,
non-inheritable Job Object before handler launch. The job disallows breakaway
and kills its members when its sole handle closes, including abrupt parent
death. Normal stopping captures member handles from that owned job, verifies
membership, terminates the job, waits captured process handles within one
shared deadline, checks accounting reaches zero and closes handles. It never
scans the machine for PIDs or kills a process by a later PID lookup. Query,
containment or exit-confirmation failure stops the poller conservatively.
See [Microsoft Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects)
and [asynchronous process termination](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminateprocess).

Linux source installs `PR_SET_PDEATHSIG` and checks for the startup parent-death
race. It kills/reaps the direct child only. Descendants do not inherit that
parent-death protection. No Linux runtime/process-tree test occurred here;
production Linux requires separately verified descendant containment and service
supervision. Other operating systems fail closed before handler dispatch. See
the [Linux parent-death signal semantics](https://www.man7.org/linux/man-pages/man2/PR_SET_PDEATHSIG.2const.html).

These controls stop owned local processes; they do not prove a remote request
was cancelled, a sandbox disappeared, or a write did not happen. SDK clients
in the trusted child still have the worker's configured credentials; this is
not credential isolation or a malicious-code sandbox. Initial SDK allocations
and whole-process peak memory remain unbounded by this slice.

The watcher can stop the child while parent database I/O blocks. The parent
poller/finalization may still wait for Firestore SDK I/O. If stop persistence is
unavailable, stale-lease recovery remains the conservative authority; no fallback
success/refund or unknown-outcome retry occurs. Final DB operations retain the
existing operation-boundary versus commit-time timing window. A shutdown racing
an already accepted, fully reaped final commit cannot atomically undo that commit.

## Verification and reproduction

From this isolated backend, with the local virtual environment:

```powershell
..\.venv\Scripts\python.exe -m pytest -q tests/test_process_supervision.py --tb=short
..\.venv\Scripts\python.exe scripts/offline_tests.py
$env:FIRESTORE_EMULATOR_HOST = '127.0.0.1:8787'
..\.venv\Scripts\python.exe -m pytest -q tests/integration/test_firestore_execution.py --tb=short -o junit_family=legacy --junitxml=../verification/supervision-firestore-integration.junit.xml
..\.venv\Scripts\python.exe -m compileall -q app tests scripts
..\.venv\Scripts\python.exe -m pip check
git diff --check
```

The versioned offline harness uses synthetic provider keys and blocks Python
socket dispatch except the Windows stdlib socket-pair wake-up mechanism. Its
main guard prevents Windows spawn from re-entering pytest. `pytest.ini` supplies
the same helper import paths for full and standalone emulator invocations.
Spawned stubs never call a real model, E2B, OAuth, Stripe or production database.

Final verification: **273 offline passed, 24 emulator cases skipped**, then
**24 separate real-SDK/emulator cases passed, no skips**. The offline run has
three pre-existing memory datetime warnings; the final emulator run has none.
The full suite includes 33 actual process cases. Compileall, dependency
consistency and diff checks pass. The final emulator admission race accepted
two/rejected two for capacity; the budget race accepted three/rejected three
for quota, with no contention rejection in that run. Earlier bounded contention
failures remain availability evidence, not permission to overspend or replay.
The task-owned emulator was stopped after PID/path/start-time verification;
port 8787 has no listener.

Cases include actual hangs, elapsed deadline, cancellation/shutdown, child and
parent kill, restart without replay, lost/stale authority, terminal staging,
competing messages, descendant exit before blocked final DB work, malformed/
oversized/deep/post-done/spoofed-EOF frames, blocked parent I/O, reservation
retention, isolated cleanup and first-stop-fails/cleanup-retry-succeeds latching.
The emulator suite additionally commits real transaction-backed RPC saves,
cancellation and abrupt parent-kill recovery with retained budget reservations.
All test/emulator processes are task-owned; no unrelated process was terminated.

Evidence is under task `verification/supervision-*`: offline/process/review test
logs, emulator JUnit/logs/process identity record and patch manifest. Three
independent read-only reviewers checked recovery, security and native termination
semantics; their findings became the staging, drain, descendant-wait and failed
stop regressions. No live integrations or Linux execution were verified.
Final focused reviewer confirmations found the stop latch and validated-EOF
blockers addressed, with no new blocker in that bounded confirmation.

Next dependency: durable per-action dispatch/result receipts and explicit
uncertain-outcome reconciliation, reusing existing repository/budget authority.
Workspace snapshot restore and remaining frontend contracts stay in the same
sequential checklist. Keep GitHub writes disabled and the worker guarded.


Subsequent receipt slice: `ACTION_RECEIPTS.md` now records stable tool intents,
fenced permits/results and owner reconciliation evidence through this same parent
RPC. Verified locally at 309 offline and 35 emulator cases; remote cancellation,
Linux containment and parent finalization limits above remain unchanged.

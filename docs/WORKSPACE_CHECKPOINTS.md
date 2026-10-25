# Workspace checkpoint and restore foundation

This is a local, bounded persistence foundation. It does **not** restore an E2B
sandbox, adopt files into an active workspace, resume an interrupted agent, or
provide production backup. There is no API route, child RPC, worker hook, runtime
configuration switch, or default adapter enabling it. Constructing
`WorkspacePersistence` without both explicit adapters raises
`PersistenceUnavailable`. Keep the experimental worker guard and GitHub write
tool restrictions.

## Audit of existing persistence and lifecycle

- `app/store.py` stores session messages, model/plan/approval state and execution
  metadata in Firestore. The command response snapshot and `/stream` snapshot
  are JSON state, not copies of workspace files. Compaction is not file backup.
- `SandboxManager` creates/connects E2B sandboxes via a session registry. There
  is no filesystem export, immutable artifact store, configured workspace root,
  or verified restore/adoption operation in that manager. Create can return late;
  local process termination cannot prove a remote sandbox disappeared.
- Cleanup retains the registry when connect/kill fails. Interrupted execution
  keeps cleanup intents and reservations for reconciliation. This slice neither
  deletes remote resources nor changes those uncertainty rules.
- The existing TypeScript server was read at pinned commit
  `89a1bcac115e26a07c8990a3f491e84ee278b2ff`, especially
  [storage/snapshots.ts](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/storage/snapshots.ts),
  [projects/persistence.ts](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/projects/persistence.ts),
  and [storage/maintenance.ts](https://github.com/pdevulapally/lotusbuild-server/blob/89a1bcac115e26a07c8990a3f491e84ee278b2ff/src/storage/maintenance.ts).
  It uses immutable candidate objects, exact storage generations, a transactional
  publish boundary and durable upload accounting/cleanup. Missing saved content
  fails restore instead of becoming an empty repository. Its artifact is a
  compressed `.git` directory reconstructed into a working tree. These verified
  invariants informed this foundation; its tar commands and provider integration
  were not copied or represented as safe Python restore support.

## Authority and publication

`WorkspaceTransitions` extends the existing `RunRepository`. It shares the same
transaction runner, owner checks, run lease/fence, cancellation/deadline checks,
admission slots, session storage bounds and lifecycle events. It does not add
authentication, a runner, a budget ledger, or a separate session store.

Checkpoint receipts use `agent_v2_workspace_checkpoints`; restore receipts use
`agent_v2_workspace_restores`. These are local schema additions, not deployment
or migration instructions. Firestore holds bounded metadata and digests, not
file contents, filenames, source paths or credentials.

A checkpoint begins only under a running admitted claim, with no recovery flag,
uncertain action or cleanup intent. Its idempotency identity binds session, run
and key; its receipt also binds owner/fence, source digest, generation and an
explicit expiry. A durable session operation lock excludes competing checkpoint
keys, action permits, normal saves/finishes and later commands. Heartbeats and
cancellation continue through the existing authority.

An effectful action permit conservatively increments the parent-owned workspace
generation before dispatch. This includes tools whose effect is not necessarily
a file write. Handler failure or missing return never rolls that generation
back. Child session saves preserve current workspace authority fields, just as
they preserve action sequence. This counter fences tool permits; it cannot
observe untracked background processes or external writers. Production needs
verified provider quiescence as well.

The adapter uploads immutable raw file objects, then a canonical version-1 JSON
manifest. The service reads back the manifest and every referenced object,
verifies hashes and sizes, and only then transactionally changes `staging` to
`ready` and updates the session checkpoint head. Expiry, ownership, fence,
generation and action uncertainty are rechecked at publication. Uploaded objects
and a completion marker alone confer no restore authority. Missing/corrupt data
fails closed; it never initializes an empty replacement.

Exact completed snapshot retries verify stored bytes without recapturing files.
Staging upload retries require the same live claim/source/key/expiry and the
adapter's stable quiesced source contract; immutable objects reject changed bytes
at an existing key. A lost publication reply can be retried with the exact sealed
metadata. Expired/stale worker authority cannot publish or reclaim a running run.

## Format, paths and privacy

- Raw regular files only: no tar/zip extraction, commands, Git checkout, hooks,
  ownership restoration or executable permission restoration. An archive inside
  a workspace is an opaque bounded file and is never decompressed.
- Technical ceilings: 256 files, 2 MiB per file, 16 MiB total, 128 KiB manifest,
  16 path segments and 240 UTF-8 path bytes. Tests may choose stricter limits.
  These are format/admission safety bounds, not commercial quota changes.
- The portable path subset is ASCII. Absolute paths, traversal, backslashes,
  empty segments, Windows device names, ADS syntax, trailing dots/spaces,
  control characters, case collisions and file/directory collisions reject.
  Local fixtures reject symlinks, Windows reparse points/junctions, hardlinks
  and special files. Input counts, traversal and reads are bounded.
- `.env*`, Git internals, credential/config directories, dependency/virtualenv
  trees, common credential files and private-key extensions are excluded.
  Private-key and common JSON/env secret assignments also fail content checks.
  These conservative exclusions are incomplete by nature: arbitrary credentials
  embedded in source cannot be reliably identified by names or regex. An
  approved production content/selection policy is a prerequisite. No content or
  filenames are logged to lifecycle events.

## Restore, quarantine and recovery

Restore only materializes into an exclusively allocated private candidate for a
fresh admitted create run before sandbox provisioning. It rejects an existing
target registry, cleanup intent, prior workspace generation/action history or
recovery flag. The source must belong to the same UID, name the current ready
checkpoint and generation, remain unexpired, and have no active run/operation.
A failed source may still yield its current known files into quarantine; this
does not establish outcomes of its uncertain actions.

Files are verified and fsynced before a separate completion marker. Exact-tree
verification rejects extra/missing files or directories, links and altered bytes.
The final metadata transaction again checks source activity/generation/expiry,
target registry, lease and cancellation. If any check fails, the files remain
quarantined and the receipt stays `staging`.

`materialized` means verified candidate files only. It is never live adoption or
agent success. The session retains a restore receipt pointer which blocks normal
tools/saves/finishes/new turns until a future verified adoption or reconciliation
path exists. It does not clear recovery, grant an approval, replay a tool, settle
model usage, refund spend, or release capacity. A same-key completed retry checks
the exact candidate again. Partial restore retries refuse to overwrite/replay;
other keys also cannot bypass the retained operation lock.

No automatic lock expiry, deletion, retention sweeper or unsafe recovery/reset
endpoint is provided. Expiry is a strict authorization boundary (`now >= expiry`
rejects); it does not delete bytes. Old/unpublished objects and partial candidates
remain for separately authorized reconciliation. Approved retention, deletion,
orphan accounting and storage admission policy are still required. No real user
workspace data was captured, restored, migrated or deleted during this work.

## Adapter boundary and exact production prerequisites

`ObjectStore` requires private, immutable, bounded, durable, create-only exact-byte
I/O. `WorkspaceAdapter` requires a stable quiesced export and exclusive safe
candidate materialization/verification. `LocalHarness` implements these only for
fresh task-owned fixture directories. Its validated `reopen` supports a fresh
local process. It never accepts arbitrary user workspace roots. Test directories
must have no external writers. The harness's lstat/fstat checks are not hostile
concurrent filesystem containment, and file fsync is not a verified power-loss
guarantee for directory metadata on every filesystem.

Before production integration, supply and verify all of:

1. A configured workspace/provider root and authoritative registry/source
   binding; quiesced no-follow export under hostile writers; supervised bounded
   provider operations; fresh-sandbox candidate adoption with atomic registry
   generation/fence checks. No real E2B restore SDK contract was verified here.
2. Private durable object storage with reachability/access checks, immutable
   object versions/create preconditions and verified checksum/length reads.
   Firebase Storage configuration/permissions/credentials were not presumed.
3. Existing product storage-byte admission/accounting and approved content,
   retention/deletion and reconciliation policies. Do not invent paid allowances
   or use the local byte ceiling as billing authority.
4. Parent-owned integration with supervised execution/receipts and explicit
   restore/adoption lifecycle, then isolated provider/storage fault rehearsal.
   The current child RPC intentionally cannot request these operations.

## Local verification

The local suite covers actual fixture file roundtrips, immutable upload retry,
lost publication acknowledgment, corrupt manifest/hash, wrong tenant, stale lease,
active/newer source, existing target registry, cancellation, competing operations,
session bounds, Windows junction and hardlink rejection, extra restore entries,
secret exclusions, raw archive handling and exact retention expiry with a live
target lease. A fresh child process reopens the store and exits during partial
restore; the parent confirms no completion marker and no overwrite on retry.
Other fault points use deterministic exception injection.

The separate Firestore emulator cases exercise real SDK transactions for
publication/materialization, owner/generation/activity/retention gates, stale
upload authority, cancellation and competing checkpoints. No fake transaction
adapter is used for that run. Neither these tests nor Windows fixture files
establish remote provider restore, managed Firestore load, hostile filesystem
isolation, Linux containment or production power-loss durability.

Final counts and exact hashed artifacts are recorded in `NEXT_TASK_HANDOFF.md`
and task-5 `workspace-checkpoint-manifest.json`.

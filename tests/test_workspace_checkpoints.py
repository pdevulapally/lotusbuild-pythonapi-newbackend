"""Real private fixture files + transactional state tests; no provider restore."""
from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest

from test_durable_runs import env, create, doc
from app.execution.repository import JOBS_COLLECTION, LEASE_SECONDS, LeaseLost, CommandError
from app.execution.errors import ExecutionStopped, OutputLimitExceeded
from app.execution.slots import SLOTS_COLLECTION
from app.execution.workspaces import CHECKPOINTS_COLLECTION, RESTORES_COLLECTION
from app.sandbox.checkpoints import (
    CheckpointInvalid, Limits, LocalHarness, PersistenceUnavailable, WorkspacePersistence,
    digest, excluded, safe_path, validate_manifest,
)
from app.store import SESSIONS_COLLECTION


def running(repo, key="source", uid="alice"):
    session = repo.submit(uid, key, "create", {"task": "fixture", "kind": "code"})
    claim = repo.claim(session["active_run_id"], "worker-" + key)
    repo.start(claim)
    return session, claim


def finish(repo, db, claim):
    current = db.collection(SESSIONS_COLLECTION).document(claim.session_id).get().to_dict()
    current["status"] = "idle"
    repo.save(claim, current)


def prepared(env, tmp_path, *, limits=None, files=None):
    repo, db, clock = env
    local = LocalHarness(tmp_path / "private")
    service = WorkspacePersistence(repo, local, local, limits=limits)
    source = local.fixture(files or {"src/app.py": b"print('saved work')", "README.md": b"work"})
    session, claim = running(repo)
    receipt = service.snapshot(claim, key="save", source=source, expires_at=clock[0] + 100)
    return service, local, source, session, claim, receipt


def test_real_files_roundtrip_idempotent_and_quarantined(env, tmp_path):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path,
        files={"src/a.py": b"uncommitted work", ".env": b"secret", ".git/config": b"secret", "empty": b""})
    assert receipt["file_count"] == 2
    assert service.snapshot(claim, key="save", source=source, expires_at=1100.0) == receipt
    finish(repo, db, claim)
    target, target_claim = running(repo, "target")
    slots = doc(db, SLOTS_COLLECTION, "alice")
    result = service.restore(target_claim, checkpoint_id=receipt["id"], key="restore")
    assert result["state"] == "materialized"
    root = local.candidates / result["id"]
    assert (root / "src/a.py").read_bytes() == b"uncommitted work"
    assert not (root / ".env").exists()
    assert not (root / ".git").exists()
    assert service.restore(target_claim, checkpoint_id=receipt["id"], key="restore") == result
    assert doc(db, SLOTS_COLLECTION, "alice") == slots
    current = doc(db, SESSIONS_COLLECTION, target["id"])
    assert current["status"] == "running" and not current["recovery_required"]
    assert "workspace_checkpoint_id" not in current  # Never claims live adoption.
    with pytest.raises(LeaseLost):
        repo.finish(target_claim)
    with pytest.raises(LeaseLost):
        repo.save(target_claim, {**current, "status": "idle"})


@pytest.mark.parametrize("path", ["../escape", "/absolute", "a/../../out", "C:/out", "a\\out", "a//b", "./a", "CON", "aux.txt", "a.", "a ", "a:b", "a/../b", "x\x00y", "a/" * 17 + "z", "x" * 241])
def test_unsafe_paths_rejected(path):
    with pytest.raises(CheckpointInvalid):
        safe_path(path)


@pytest.mark.parametrize("path", [".env", "src/.env.local", ".ssh/id_ed25519", "key.pem", ".aws/config", ".npmrc", ".git/objects/a", "src/service-account.json"])
def test_secret_exclusion(path):
    assert excluded(path)


def test_production_has_no_default_adapter(env):
    with pytest.raises(PersistenceUnavailable):
        WorkspacePersistence(env[0])


@pytest.mark.parametrize("files,limits", [({"a": b"x" * 11}, Limits(file_bytes=10)),
    ({"a": b"x" * 6, "b": b"x" * 6}, Limits(total_bytes=10)),
    ({"a": b"1", "b": b"2"}, Limits(files=1)),
    ({"a": b"-----BEGIN PRIVATE KEY-----"}, Limits()),
    ({"a": b"API_KEY=abcdefghijklm"}, Limits())])
def test_limits_and_secret_content_never_publish(env, tmp_path, files, limits):
    repo, db, clock = env
    local = LocalHarness(tmp_path / "private")
    service = WorkspacePersistence(repo, local, local, limits=limits)
    source = local.fixture(files)
    _, claim = running(repo)
    with pytest.raises(CheckpointInvalid):
        service.snapshot(claim, key="save", source=source, expires_at=1100.0)
    assert all(v["state"] == "staging" for (c, _), v in db.data.items() if c == CHECKPOINTS_COLLECTION)


def test_partial_upload_crash_never_publishes_and_safe_exact_retry(env, tmp_path, monkeypatch):
    repo, db, clock = env
    local = LocalHarness(tmp_path / "private")
    service = WorkspacePersistence(repo, local, local)
    source = local.fixture({"a": b"first", "b": b"second"})
    _, claim = running(repo)
    put = local.put
    calls = [0]
    def crash(key, data):
        put(key, data)
        calls[0] += 1
        if calls[0] == 1:
            raise KeyboardInterrupt("simulated crash after durable upload")
    monkeypatch.setattr(local, "put", crash)
    with pytest.raises(KeyboardInterrupt):
        service.snapshot(claim, key="save", source=source, expires_at=1100.0)
    assert all(v["state"] == "staging" for (c, _), v in db.data.items() if c == CHECKPOINTS_COLLECTION)
    monkeypatch.setattr(local, "put", put)
    result = service.snapshot(claim, key="save", source=source, expires_at=1100.0)
    assert result["state"] == "ready"


@pytest.mark.parametrize("which", ["manifest", "file"])
def test_corruption_rejects_without_empty_fallback(env, tmp_path, which):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    manifest = service._manifest(receipt)
    suffix = "manifest" if which == "manifest" else manifest["files"][0]["sha256"]
    key = service._key(receipt, suffix)
    (local.objects / key).write_bytes(b"corrupted")
    finish(repo, db, claim)
    _, target = running(repo, "target")
    with pytest.raises(CheckpointInvalid):
        service.restore(target, checkpoint_id=receipt["id"], key="restore")
    assert not list(local.candidates.iterdir())


@pytest.mark.parametrize("mutation", ["version", "traversal", "duplicate", "hash", "oversize", "tenant", "collision"])
def test_untrusted_manifest_even_with_matching_digest(env, tmp_path, mutation):
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    value = service._manifest(receipt)
    if mutation == "version": value["version"] = 2
    if mutation == "traversal": value["files"][0]["path"] = "../escape"
    if mutation == "duplicate": value["files"][1]["path"] = value["files"][0]["path"]
    if mutation == "hash": value["files"][0]["sha256"] = "bad"
    if mutation == "oversize": value["files"][0]["bytes"] = 2 ** 40
    if mutation == "tenant": value["uid"] = "bob"
    if mutation == "collision": value["files"][1]["path"] = value["files"][0]["path"] + "/x"
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    with pytest.raises(CheckpointInvalid):
        validate_manifest(raw, {**receipt, "manifest_digest": digest(raw)}, service.limits)


def test_symlink_and_hardlink_rejected(env, tmp_path):
    repo, db, clock = env
    local = LocalHarness(tmp_path / "private")
    source = local.fixture({"a": b"data"})
    root = local.fixtures / source
    # Hard links are available without Windows symlink privilege.
    os.link(root / "a", root / "b")
    with pytest.raises(CheckpointInvalid):
        list(local.capture(source, Limits()))


def test_wrong_tenant_expiry_and_active_source_rejected(env, tmp_path):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    _, other = running(repo, "bob", uid="bob")
    with pytest.raises(LeaseLost):
        service.restore(other, checkpoint_id=receipt["id"], key="no")
    _, target = running(repo, "target")
    with pytest.raises(LeaseLost):
        service.restore(target, checkpoint_id=receipt["id"], key="active")
    finish(repo, db, claim)
    clock[0] = 1044.0
    repo.heartbeat(target)
    clock[0] = 1088.0
    repo.heartbeat(target)
    clock[0] = receipt["expires_at"]  # Live target lease; exact retention boundary.
    with pytest.raises(LeaseLost):
        service.restore(target, checkpoint_id=receipt["id"], key="expired")


def test_stale_lease_cannot_publish(env, tmp_path, monkeypatch):
    repo, db, clock = env
    local = LocalHarness(tmp_path / "private")
    service = WorkspacePersistence(repo, local, local)
    source = local.fixture({"a": b"work"})
    _, claim = running(repo)
    put = local.put
    def lose(key, data):
        put(key, data)
        clock[0] += LEASE_SECONDS
    monkeypatch.setattr(local, "put", lose)
    with pytest.raises(LeaseLost):
        service.snapshot(claim, key="save", source=source, expires_at=1200.0)
    assert all(v["state"] == "staging" for (c, _), v in db.data.items() if c == CHECKPOINTS_COLLECTION)


def test_partial_restore_is_quarantined_without_retry_or_replay(env, tmp_path, monkeypatch):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    finish(repo, db, claim)
    target_session, target = running(repo, "target")
    materialize = local.materialize
    def crash(restore_id, files, checkpoint):
        materialize(restore_id, files[:1], checkpoint)
        raise KeyboardInterrupt("simulated process death during restore")
    monkeypatch.setattr(local, "materialize", crash)
    slots = doc(db, SLOTS_COLLECTION, "alice")
    with pytest.raises(KeyboardInterrupt):
        service.restore(target, checkpoint_id=receipt["id"], key="restore")
    assert all(v["state"] == "staging" for (c, _), v in db.data.items() if c == RESTORES_COLLECTION)
    with pytest.raises(LeaseLost):
        service.restore(target, checkpoint_id=receipt["id"], key="restore")
    with pytest.raises(LeaseLost):
        service.restore(target, checkpoint_id=receipt["id"], key="another")
    assert doc(db, SLOTS_COLLECTION, "alice") == slots
    assert doc(db, SESSIONS_COLLECTION, target_session["id"])["workspace_operation"]


def test_snapshot_lock_and_restore_race_conflict(env, tmp_path):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    finish(repo, db, claim)
    _, target = running(repo, "target")
    def begin(key):
        try:
            return repo.begin_workspace_restore(target, checkpoint_id=receipt["id"], key=key)
        except LeaseLost:
            return None
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(begin, ["one", "two"]))
    assert sum(r is not None for r in results) == 1


def test_changed_source_retry_rejected(env, tmp_path):
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    another = local.fixture({"a": b"different workspace"})
    with pytest.raises(LeaseLost, match="retry changed"):
        service.snapshot(claim, key="save", source=another, expires_at=1100.0)


@pytest.mark.parametrize("data", [b'{"api_key":"abcdefghijk"}', b'{"password": "abcdefghijk"}',
    b"secret = 'abcdefghijk'", b"access_token=abcdefghijk"])
def test_secret_json_and_assignments_rejected(env, tmp_path, data):
    repo, db, clock = env
    local = LocalHarness(tmp_path / "private")
    source = local.fixture({"config.json": data})
    _, claim = running(repo)
    with pytest.raises(CheckpointInvalid, match="secret"):
        WorkspacePersistence(repo, local, local).snapshot(claim, key="save", source=source, expires_at=1100.0)


def test_actual_windows_junction_escape_rejected(env, tmp_path):
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows junction case")
    local = LocalHarness(tmp_path / "private")
    source = local.fixture({"a": b"data"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "confidential").write_bytes(b"outside fixture")
    junction = local.fixtures / source / "escape"
    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    assert result.returncode == 0, result.stderr
    with pytest.raises(CheckpointInvalid, match="links"):
        list(local.capture(source, Limits()))


@pytest.mark.parametrize("fault", ["source_active", "new_generation", "cancel", "lease", "registry"])
def test_restore_commit_rechecks_after_local_marker(env, tmp_path, monkeypatch, fault):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    finish(repo, db, claim)
    target_session, target = running(repo, "target")
    materialize = local.materialize
    def race(restore_id, files, checkpoint):
        materialize(restore_id, files, checkpoint)
        if fault == "source_active":
            repo.submit("alice", "new", "message", {"content": "new work"}, session["id"])
        elif fault == "new_generation":
            db.data[(SESSIONS_COLLECTION, session["id"])]["workspace_generation"] = 1
        elif fault == "cancel":
            repo.cancel(target_session["id"], "alice")
        elif fault == "lease":
            clock[0] += LEASE_SECONDS
        elif fault == "registry":
            db.data[("agent_v2_sandbox_registry", target_session["id"])] = {"sandbox_id": "already-provisioned"}
    monkeypatch.setattr(local, "materialize", race)
    with pytest.raises(ExecutionStopped):
        service.restore(target, checkpoint_id=receipt["id"], key="restore")
    assert all(v["state"] == "staging" for (c, _), v in db.data.items() if c == RESTORES_COLLECTION)
    assert list(local.candidates.glob("*.complete"))  # Complete files, still quarantined metadata.
    assert target.job_id in doc(db, SLOTS_COLLECTION, "alice")["runs"]


@pytest.mark.parametrize("extra", ["extra.py", "extra-dir"])
def test_idempotent_restore_rejects_unmanifested_entries(env, tmp_path, extra):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    finish(repo, db, claim)
    _, target = running(repo, "target")
    result = service.restore(target, checkpoint_id=receipt["id"], key="restore")
    path = local.candidates / result["id"] / extra
    if extra == "extra-dir": path.mkdir()
    else: path.write_bytes(b"unexpected")
    with pytest.raises(CheckpointInvalid, match="unmanifested"):
        service.restore(target, checkpoint_id=receipt["id"], key="restore")


def test_checkpoint_lock_blocks_action_and_mutation_advances_generation(env, tmp_path):
    from test_action_receipts import CALL
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    batch = repo.prepare_actions(claim, step=0, calls=[CALL])
    repo.begin_workspace_checkpoint(claim, key="second", source_digest="a" * 64, expires_at=1100.0)
    with pytest.raises(LeaseLost, match="workspace"):
        repo.permit_action(claim, action_id=batch["action_ids"][0], name=CALL["name"], input=CALL["input"])
    with pytest.raises(LeaseLost):
        repo.begin_workspace_checkpoint(claim, key="competing", source_digest="a" * 64, expires_at=1100.0)
    # A failed worker cannot silently clear this operation or release capacity.
    repo.finish(claim, failed=True)
    assert claim.job_id in doc(db, SLOTS_COLLECTION, "alice")["runs"]


def test_permitted_mutation_invalidates_old_checkpoint_and_child_cannot_reset_generation(env, tmp_path):
    from test_action_receipts import CALL
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    batch = repo.prepare_actions(claim, step=0, calls=[CALL])
    aid = batch["action_ids"][0]
    repo.permit_action(claim, action_id=aid, name=CALL["name"], input=CALL["input"])
    assert doc(db, SESSIONS_COLLECTION, session["id"])["workspace_generation"] == 1
    repo.record_action_result(claim, action_id=aid, result_digest="a" * 64)
    state = batch["session"]
    state.update(status="idle", workspace_generation=0, workspace_checkpoint_id="b" * 64)
    repo.save(claim, state)
    assert state["workspace_generation"] == 1 and state["workspace_checkpoint_id"] == receipt["id"]
    _, target = running(repo, "target")
    with pytest.raises(LeaseLost, match="stale"):
        service.restore(target, checkpoint_id=receipt["id"], key="restore")


def test_actual_fresh_process_store_reopen_and_crash_candidate(env, tmp_path):
    import subprocess
    import sys
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    code = '''
import os, sys
from app.sandbox.checkpoints import LocalHarness, validate_manifest, Limits
local = LocalHarness.reopen(sys.argv[1])
receipt = __import__('json').loads(sys.argv[2])
key = sys.argv[3]
validate_manifest(local.read(key, 131072), receipt, Limits())
calls = 0
def crash():
    global calls
    calls += 1
    if calls == 2:
        os._exit(17)
local.materialize('a' * 64, [('a', b'partial'), ('b', b'not-written')], crash)
'''
    process = subprocess.run([sys.executable, "-c", code, str(local.root), json.dumps(receipt),
                              service._key(receipt, "manifest")], capture_output=True, timeout=20)
    assert process.returncode == 17, process.stderr.decode()
    reopened = LocalHarness.reopen(local.root)
    assert (reopened.candidates / ("a" * 64) / "a").read_bytes() == b"partial"
    assert not (reopened.candidates / ("a" * 64) / "b").exists()
    assert not (reopened.candidates / ("a" * 64 + ".complete")).exists()
    assert WorkspacePersistence(env[0], reopened, reopened).snapshot(claim, key="save", source=source, expires_at=1100.0) == receipt
    with pytest.raises(FileExistsError):
        reopened.materialize("a" * 64, [("a", b"overwrite")], lambda: None)


def test_archive_is_opaque_and_never_decompressed(env, tmp_path):
    import io
    import zipfile
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("../../escape", b"x" * 100000)
    service, local, source, session, claim, receipt = prepared(env, tmp_path, files={"payload.zip": buffer.getvalue()})
    assert receipt["total_bytes"] == len(buffer.getvalue())
    assert not (tmp_path / "escape").exists()


def test_metadata_uses_shared_session_bound_and_rolls_back(env):
    repo, db, clock = env
    session, claim = running(repo)
    db.data[(SESSIONS_COLLECTION, session["id"])]["padding"] = "x" * 512000
    with pytest.raises(OutputLimitExceeded):
        repo.begin_workspace_checkpoint(claim, key="save", source_digest="a" * 64, expires_at=1100.0)
    assert not [v for (c, _), v in db.data.items() if c == CHECKPOINTS_COLLECTION]
    assert "workspace_operation" not in doc(db, SESSIONS_COLLECTION, session["id"])


def test_lost_publish_ack_allows_exact_metadata_retry(env, tmp_path):
    repo, db, clock = env
    service, local, source, session, claim, receipt = prepared(env, tmp_path)
    assert repo.publish_workspace_checkpoint(claim, checkpoint_id=receipt["id"],
        manifest_digest=receipt["manifest_digest"], file_count=receipt["file_count"],
        total_bytes=receipt["total_bytes"]) == receipt
    with pytest.raises(LeaseLost, match="changed bytes"):
        repo.publish_workspace_checkpoint(claim, checkpoint_id=receipt["id"],
            manifest_digest="b" * 64, file_count=receipt["file_count"], total_bytes=receipt["total_bytes"])

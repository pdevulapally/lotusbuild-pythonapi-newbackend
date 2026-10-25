"""Importable local-only handlers for Windows spawn tests; no SDK/provider calls."""
import json
import os
from pathlib import Path
import time


def options(session):
    return json.loads(session["messages"][0]["content"][0]["text"])


def stub(job, session):
    from app.execution.context import checkpoint, save, _current

    config = options(session)
    if config.get("marker"):
        Path(config["marker"]).write_text(str(os.getpid()))
    mode = config.get("mode", "success")
    if mode == "crash":
        os._exit(17)
    if mode == "no_terminal":
        return
    if mode == "oversized_frame":
        _current.get().repository.connection.send_bytes(b"x" * 600_000)
    if mode == "deep_frame":
        _current.get().repository.connection.send_bytes(b"[" * 2000 + b"0" + b"]" * 2000)
    if mode == "replayed_sequence":
        from app.execution.supervision import _send
        _send(_current.get().repository.connection, {"kind": "checkpoint", "sequence": 999})
    if mode in {"descendant_hang", "descendant_success"}:
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                 creationflags=0x08000000 if os.name == "nt" else 0)
        Path(config["marker"] + ".descendant").write_text(str(child.pid))
        if mode == "descendant_success":
            while not Path(config["marker"] + ".allow_terminal").exists():
                time.sleep(0.02)
    if mode in {"hang", "oversized_frame", "deep_frame", "replayed_sequence", "descendant_hang"}:
        while True:
            time.sleep(1)
    if mode == "updates":
        session["plan"] = [{"step": "one", "status": "in_progress"}]
        save(session)
        session["plan"][0]["status"] = "done"
        save(session)  # Requires authoritative revision from previous RPC.
    if mode == "foreign_identity":
        session["uid"] = "different-owner"
    if mode == "approval":
        from app.execution.context import prepare_actions
        call = {"id": "synthetic", "name": "bash", "input": {"command": "sudo synthetic"}}
        batch = prepare_actions(session, 0, [call])
        session["pending_approval"] = {"id": batch["batch_id"], "batch_id": batch["batch_id"],
                                       "provider": "test-provider", "model": "test-model", "quota": None,
                                       "calls": [{**call, "risky": True, "action_id": batch["action_ids"][0]}]}
    session["status"] = "awaiting_approval" if mode == "approval" else "idle"
    save(session)
    if config.get("terminal_marker"):
        Path(config["terminal_marker"]).write_text("staged")
    if mode == "terminal_crash":
        os._exit(18)
    if mode == "after_terminal":
        checkpoint()
    if mode == "done_hang":
        from app.execution.supervision import _send
        _send(_current.get().repository.connection, {"kind": "done"})
    if mode in {"done_bad_frame", "done_spoofed_eof"}:
        from app.execution.supervision import _send
        connection = _current.get().repository.connection
        _send(connection, {"kind": "done"})
        if mode == "done_bad_frame":
            connection.send_bytes(b"{")
        else:
            _send(connection, {"kind": "transport_eof"})
    if mode in {"terminal_hang", "done_hang"}:
        while True:
            time.sleep(1)


def cleanup_success(job, session):
    Path(job["session_id"]).write_text(str(os.getpid()))


def cleanup_hang(job, session):
    cleanup_success(job, session)
    while True:
        time.sleep(1)


def owner_parent(connection, marker):
    """Real test parent to kill abruptly while it owns a blocked spawned child."""
    from test_durable_runs import MemoryDB
    from app.execution.repository import RunRepository
    from app.worker import process_claim

    db = MemoryDB()
    repo = RunRepository(db, transaction_runner=db.transaction)
    session = repo.submit("synthetic", "one", "create", {
        "task": json.dumps({"mode": "hang", "marker": marker}), "kind": "code"})
    claim = repo.claim(session["active_run_id"], "owner")
    process_claim(repo, claim, stub, on_started=lambda process: connection.send(process.pid))


def emulator_owner_parent(connection, job_id, clock_value):
    """Separate killed worker parent; explicit demo emulator and no Firebase/ADC."""
    from scripts.emulator_endpoint import require_endpoint
    require_endpoint(os.environ.get("FIRESTORE_EMULATOR_HOST"))
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.firestore_v1 import Client
    from app.execution.repository import RunRepository
    from app.worker import process_claim

    client = Client(project="demo-lotusbuild-local", credentials=AnonymousCredentials())
    repo = RunRepository(client, clock=lambda: clock_value)
    claim = repo.claim(job_id, "killed-test-worker")
    process_claim(repo, claim, stub, on_started=lambda process: connection.send(process.pid))
    client.close()


def action_stub(job, session):
    """Real local effect with precise durable intent/permit/result crash windows."""
    from dataclasses import replace
    import app.agent.tools as tools
    from app.execution.context import action, prepare_actions, save
    config = options(session)
    mode = config["mode"]
    call = {"id": "one", "name": "write_file", "input": {"path": "synthetic", "content": "x"}}
    batch = prepare_actions(session, 0, [call])
    aid = batch["action_ids"][0]
    if mode == "prepared_crash":
        os._exit(41)
    if mode == "permit_crash":
        action("permit_action", action_id=aid, name=call["name"], input=call["input"])
        os._exit(42)
    def effect(*args):
        marker = Path(config["marker"])
        marker.write_text((marker.read_text() if marker.exists() else "") + "effect\n")
        if mode == "effect_crash":
            os._exit(43)
        return "synthetic handler returned"
    tools.TOOL_SPECS = [replace(spec, handler=effect) if spec.name == call["name"] else spec for spec in tools.TOOL_SPECS]
    tools.dispatch_tool_call(session, object(), call["name"], call["input"], aid)
    if mode == "returned_crash":
        os._exit(44)
    if mode == "duplicate":
        tools.dispatch_tool_call(session, object(), call["name"], call["input"], aid)
    session["status"] = "idle"
    save(session)


def forged_plan_stub(job, session):
    """Child-side Build forgery cannot cross parent capability/save authority."""
    from app.execution.context import prepare_actions, save
    config = options(session)
    session["mode"] = "build"
    if config["mode"] == "save":
        save(session)
    else:
        prepare_actions(session, 0, [{"id": "write", "name": "write_file",
                        "input": {"path": "fixture", "content": "fixture"}}])
    Path(config["marker"]).write_text("bypassed")

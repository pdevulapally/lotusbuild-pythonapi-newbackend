"""Current-client contract fixtures and native authority regressions, offline only."""
import copy
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.auth as auth
import app.api.contracts as transport
from app.execution.contracts import ACCOUNTS_COLLECTION, PROJECTS_COLLECTION, MESSAGES_COLLECTION
from app.execution.repository import CommandError, JOBS_COLLECTION, RECEIPTS_COLLECTION, EVENTS_COLLECTION, LeaseLost
from app.execution.errors import OutputLimitExceeded
from app.execution.limits import MAX_SESSION_BYTES
from app.store import SESSIONS_COLLECTION
from test_durable_runs import create, doc, env, idle


CAPABILITIES = [{"capabilityKey": "platform.web", "value": {"kind": "boolean", "value": True}},
                {"capabilityKey": "projects.max", "value": {"kind": "number", "value": 3}},
                {"capabilityKey": "ai.run", "value": {"kind": "boolean", "value": True}},
                {"capabilityKey": "ai.model.allowlist", "value": {"kind": "stringList", "value": []}}]


def configure(db, maximum=3, web=True):
    capabilities = copy.deepcopy(CAPABILITIES)
    capabilities[0]["value"]["value"] = web
    capabilities[1]["value"]["value"] = maximum
    plan = {"limits": {"requests_per_day": 100, "tokens_per_month": 100000},
            "models": [{"provider": "anthropic", "model": "fixture-model", "quota": None}],
            "entitlements": capabilities}
    db.data[("plans", "free")] = plan
    return plan


def project_session(repo, project, key="run"):
    return repo.submit("alice", key, "create", {"task": "original task", "kind": "code", "model": "fixture-model", "project_id": project["id"]})


def running(repo, session):
    claim = repo.claim(session["active_run_id"], "worker")
    _, current = repo.start(claim)
    return claim, current


def assistant(repo, claim, current, text):
    message = {"role": "assistant", "content": [{"type": "text", "text": text}]}
    current["messages"].append(message)
    current["_transcript_append"] = [message]
    repo.save(claim, current)


def assert_error(status, operation):
    with pytest.raises(CommandError) as failure:
        operation()
    assert failure.value.status == status


def test_project_create_is_atomic_repeatable_and_normalized(env):
    repo, db, _ = env
    configure(db)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: repo.create_project("alice", "same", {"name": " hello "}), range(4)))
    assert all(value == results[0] for value in results)
    assert results[0]["name"] == "hello" and results[0]["platform"] == "web"
    assert results[0]["publicPreviewEnabled"] is False and "uid" not in results[0]
    assert doc(db, ACCOUNTS_COLLECTION, "alice")["projectCount"] == 1
    assert len([1 for c, _ in db.data if c == PROJECTS_COLLECTION]) == 1
    before = copy.deepcopy(db.data)
    assert_error(409, lambda: repo.create_project("alice", "same", {"name": "changed"}))
    assert db.data == before


def test_project_limit_race_has_no_rejected_receipts(env):
    repo, db, _ = env
    configure(db, maximum=1)
    def attempt(key):
        try:
            return repo.create_project("alice", key, {"name": "fixture"})
        except CommandError as error:
            return error.status
    with ThreadPoolExecutor(4) as pool:
        values = list(pool.map(attempt, ["one", "two", "three", "four"]))
    assert sum(isinstance(value, dict) for value in values) == 1
    assert values.count(403) == 3
    assert doc(db, ACCOUNTS_COLLECTION, "alice")["projectCount"] == 1
    assert len([1 for c, _ in db.data if c == RECEIPTS_COLLECTION]) == 1


@pytest.mark.parametrize("configuration", [None, [], CAPABILITIES[:1], CAPABILITIES + CAPABILITIES[:1],
    [{"capabilityKey": "platform.web", "value": {"kind": "boolean", "value": 1}}, CAPABILITIES[1]],
    [CAPABILITIES[0], {"capabilityKey": "projects.max", "value": {"kind": "number", "value": True}}]])
def test_missing_or_malformed_project_policy_fails_closed_without_grants(env, configuration):
    repo, db, _ = env
    configure(db)["entitlements"] = configuration
    before = copy.deepcopy(db.data)
    assert_error(503, lambda: repo.create_project("alice", "one", {"name": "fixture"}))
    assert db.data == before


@pytest.mark.parametrize("maximum,web", [(0, True), (3, False)])
def test_verified_denied_policy_is_not_configuration_failure(env, maximum, web):
    repo, db, _ = env
    configure(db, maximum, web)
    assert_error(403, lambda: repo.create_project("alice", "one", {"name": "fixture"}))


@pytest.mark.parametrize("payload", [{"name": " "}, {"name": "a\nforged"}, {"name": "x"*101},
    {"name": None}, {"name": "x", "platform": "mobile"}, {"name": "x", "description": None}])
def test_project_input_is_narrow_and_validated(env, payload):
    repo, db, _ = env
    configure(db)
    assert_error(422, lambda: repo.create_project("alice", "one", payload))


def test_project_history_newest_first_pagination_and_owner_scope(env):
    repo, db, _ = env
    configure(db, maximum=10)
    projects = [repo.create_project("alice", str(n), {"name": str(n)}) for n in range(5)]
    page = repo.list_projects("alice", limit=2)
    assert [p["name"] for p in page["projects"]] == ["4", "3"]
    repo.create_project("alice", "new", {"name": "new"})
    page2 = repo.list_projects("alice", page["nextCursor"], limit=2)
    assert [p["name"] for p in page2["projects"]] == ["2", "1"]
    page3 = repo.list_projects("alice", page2["nextCursor"], limit=2)
    assert [p["name"] for p in page3["projects"]] == ["0"] and page3["nextCursor"] is None
    assert repo.list_projects("mallory")["projects"] == []
    assert_error(404, lambda: repo.list_projects("mallory", projects[0]["id"], limit=0))
    assert_error(404, lambda: repo.read_project(projects[0]["id"], "mallory"))
    assert_error(404, lambda: repo.update_project(projects[0]["id"], "mallory", {"active_session_id": "x"}))


@pytest.mark.parametrize("field", ["name", "createdAt", "active_session_id"])
def test_corrupt_project_list_records_fail_without_partial_success(env, field):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    db.data[(PROJECTS_COLLECTION, project["id"])].pop(field)
    assert_error(503, lambda: repo.list_projects("alice"))


@pytest.mark.parametrize("field,value", [("role", "tool_result"), ("createdAt", "bad"), ("content", None)])
def test_corrupt_transcript_record_is_not_returned_as_success(env, field, value):
    repo, db, _ = env
    session = create(repo)
    db.data[(MESSAGES_COLLECTION, session["id"] + ":0")][field] = value
    assert_error(503, lambda: repo.message_history(session["id"], "alice"))


def test_narrow_atomic_project_patch_preserves_guard_and_concurrent_fields(env):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "before", "description": "before"})
    state = db.data[(PROJECTS_COLLECTION, project["id"])]
    state["active_session_id"] = "guarded"
    with ThreadPoolExecutor(2) as pool:
        list(pool.map(lambda payload: repo.update_project(project["id"], "alice", payload), [{"name": "after"}, {"description": "after"}]))
    result = doc(db, PROJECTS_COLLECTION, project["id"])
    assert result["active_session_id"] == "guarded" and result["name"] == result["description"] == "after"
    assert "description" not in repo.update_project(project["id"], "alice", {"description": ""})
    assert_error(422, lambda: repo.update_project(project["id"], "alice", {"status": "idle"}))


def test_project_session_bindings_repeat_race_ownership_and_kind(env):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: project_session(repo, project), range(4)))
    session = results[0]
    assert all(value == session for value in results)
    assert doc(db, PROJECTS_COLLECTION, project["id"])["active_session_id"] == session["id"]
    before = copy.deepcopy(db.data)
    assert_error(409, lambda: project_session(repo, project, "second"))
    payload = {"task": "x", "project_id": project["id"]}
    assert_error(404, lambda: repo.submit("mallory", "foreign", "create", payload))
    assert_error(422, lambda: repo.submit("alice", "desktop", "create", payload | {"kind": "desktop"}))
    assert db.data == before
    idle(repo, session)
    assert_error(409, lambda: project_session(repo, project, "after-idle"))


def test_current_project_policy_is_rechecked_at_run_and_action_admission(env):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    session = project_session(repo, project)
    claim = repo.claim(session["active_run_id"], "worker")
    configure(db, web=False)
    assert_error(403, lambda: repo.start(claim))
    configure(db)
    _, current = repo.start(claim)
    configure(db, web=False)
    assert_error(403, lambda: repo.prepare_actions(claim, step=0, calls=[{"id": "read", "name": "read_file", "input": {"path": "a"}}]))
    assert not any(c == "agent_v2_actions" for c, _ in db.data)


@pytest.mark.parametrize("case,status", [("missing_ai", 503), ("denied_ai", 403), ("missing_models", 503), ("denied_model", 403)])
def test_verified_execution_capabilities_do_not_silently_ignore_deny(env, case, status):
    repo, db, _ = env
    plan = configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    if case == "missing_ai":
        plan["entitlements"] = [entry for entry in plan["entitlements"] if entry["capabilityKey"] != "ai.run"]
    elif case == "denied_ai":
        plan["entitlements"][2]["value"]["value"] = False
    elif case == "missing_models":
        plan["entitlements"] = plan["entitlements"][:3]
    else:
        plan["entitlements"][3]["value"]["value"] = ["another-model"]
    before = copy.deepcopy(db.data)
    assert_error(status, lambda: project_session(repo, project))
    assert db.data == before


@pytest.mark.parametrize("revoked", ["ai.run", "ai.model.allowlist"])
def test_project_revocation_after_reservation_stops_provider_without_refund(env, monkeypatch, revoked):
    from types import SimpleNamespace
    import app.billing.budgets as budgets
    import app.billing.entitlements as entitlements
    from app.execution.context import Guard, execution_context
    from app.billing.plans import ModelEntry
    repo, db, clock = env
    plan = configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    session = project_session(repo, project)
    claim, _ = running(repo, session)
    calls = []
    def reserve(*args, **kwargs):
        calls.append("reserved")
        for entry in plan["entitlements"]:
            if entry["capabilityKey"] == revoked:
                entry["value"]["value"] = False if revoked == "ai.run" else ["another-model"]
        return "retained-reservation"
    resolve = entitlements.resolve_entitlements
    monkeypatch.setattr(entitlements, "resolve_entitlements", lambda uid, **kwargs: resolve(uid, **(kwargs or {"client": db, "now": clock[0]})))
    monkeypatch.setattr(budgets, "request_allowance", lambda *args: 5)
    monkeypatch.setattr(budgets, "budgets", SimpleNamespace(reserve=reserve, settle=lambda *args: calls.append("settled")))
    adapter = SimpleNamespace(create=lambda **kwargs: calls.append("provider"))
    with execution_context(Guard(repo, claim)):
        assert_error(403, lambda: budgets.metered_create("alice", adapter, ModelEntry("anthropic", "fixture-model", None),
            system="fixture", messages=[], tools=[], max_tokens=1))
    assert calls == ["reserved"]
    repo.finish(claim, failed=True)
    assert doc(db, SESSIONS_COLLECTION, session["id"])["recovery_required"] is True


def test_transcript_original_text_survives_explicit_compaction_and_tool_results(env, monkeypatch):
    import app.agent.compaction as compaction
    from app.billing.plans import ModelEntry
    repo, db, _ = env
    session = create(repo)
    claim, current = running(repo, session)
    for n in range(15):
        assistant(repo, claim, current, f"original-{n}")
    archived = repo.message_history(session["id"], "alice")["messages"]
    monkeypatch.setattr(compaction, "_summarize", lambda *a: "synthetic summary")
    compaction.compact(current, ModelEntry("fixture", "fixture", None))
    repo.save(claim, current)
    assert "_context_compacted" not in current
    assert repo.message_history(session["id"], "alice")["messages"] == archived
    current["messages"].append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "private-tool-output"}]})
    repo.save(claim, current)
    assistant(repo, claim, current, "after compaction")
    rows = repo.message_history(session["id"], "alice")["messages"]
    assert rows[:-1] == archived and rows[-1]["content"] == "after compaction"
    assert not any("summary" in row["content"] or "private-tool" in row["content"] for row in rows)
    assert "_transcript_append" not in doc(db, SESSIONS_COLLECTION, session["id"])


def test_transcript_page_returns_latest_in_ascending_sequence_and_no_gaps(env):
    repo, db, _ = env
    session = create(repo)
    claim, current = running(repo, session)
    for n in range(5):
        assistant(repo, claim, current, f"{n}")
    page = repo.message_history(session["id"], "alice", limit=2)
    assert [m["seq"] for m in page["messages"]] == [4, 5] and page["nextCursor"] == "4"
    page2 = repo.message_history(session["id"], "alice", page["nextCursor"], 2)
    assert [m["seq"] for m in page2["messages"]] == [2, 3]
    assert_error(404, lambda: repo.message_history(session["id"], "mallory", "bad", 0))
    del db.data[(MESSAGES_COLLECTION, session["id"] + ":4")]
    assert_error(503, lambda: repo.message_history(session["id"], "alice", limit=2))


@pytest.mark.parametrize("field,value", [("message_count", 0), ("transcript_version", 2), ("project_id", "forged")])
def test_worker_cannot_change_history_or_project_authority(env, field, value):
    repo, db, _ = env
    session = create(repo)
    claim, current = running(repo, session)
    current[field] = value
    before = copy.deepcopy(db.data)
    with pytest.raises(LeaseLost):
        repo.save(claim, current)
    assert db.data == before


def test_transcript_create_collision_rolls_back_state_events_and_duplicate_append(env):
    repo, db, _ = env
    session = create(repo)
    claim, current = running(repo, session)
    db.data[(MESSAGES_COLLECTION, session["id"] + ":1")] = {"immutable": True}
    before = copy.deepcopy(db.data)
    with pytest.raises(ValueError):
        assistant(repo, claim, current, "new")
    assert db.data == before
    del db.data[(MESSAGES_COLLECTION, session["id"] + ":1")]
    repo.save(claim, current)
    stale = copy.deepcopy(current)
    repo.save(claim, current)
    assert len(repo.message_history(session["id"], "alice")["messages"]) == 2
    with pytest.raises(LeaseLost):
        repo.save(claim, stale)


def test_transcript_legacy_and_aggregate_size_fail_honestly(env):
    repo, db, _ = env
    session = create(repo)
    state = db.data[(SESSIONS_COLLECTION, session["id"])]
    state.pop("transcript_version")
    assert_error(503, lambda: repo.message_history(session["id"], "alice"))
    state.update(transcript_version=1, message_count=2)
    for n in range(2):
        db.data[(MESSAGES_COLLECTION, f"{session['id']}:{n}")] = {"session_id": session["id"], "uid": "alice", "seq": n,
            "role": "assistant", "content": "x" * (MAX_SESSION_BYTES//2), "attachments": [], "createdAt": session["created_at"]}
    with pytest.raises(OutputLimitExceeded):
        repo.message_history(session["id"], "alice")


def test_cancel_receipt_race_replays_snapshot_without_mutating_newer_state(env):
    repo, db, _ = env
    session = create(repo)
    claim, _ = running(repo, session)
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda _: repo.cancel(session["id"], "alice", "same"), range(3)))
    assert all(value == results[0] for value in results)
    assert results[0]["status"] == "cancel_requested"
    repo.finish(claim)
    before = copy.deepcopy(db.data)
    assert repo.cancel(session["id"], "alice", "same") == results[0]
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"
    assert_error(404, lambda: repo.cancel(session["id"], "mallory", "same"))
    assert db.data == before


@pytest.fixture
def client(env, monkeypatch):
    from app.main import app
    repo, db, _ = env
    monkeypatch.setattr(transport, "runs", repo)
    monkeypatch.setattr("app.ratelimit.check_rate_limit", lambda *args: None)
    app.dependency_overrides[auth.get_uid] = lambda: "alice"
    app.dependency_overrides[auth.get_verified_uid] = lambda: "alice"
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def test_http_project_and_message_responses_match_current_client_fields(env, client):
    repo, db, _ = env
    configure(db)
    created = client.post("/projects", json={"name": " fixture "}, headers={"Idempotency-Key": "p"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"id", "name", "platform", "status", "publicPreviewEnabled", "createdAt"}
    assert client.patch(f"/projects/{body['id']}", json={"name": "new"}).json()["name"] == "new"
    assert client.get("/projects?limit=100").json()["projects"][0]["id"] == body["id"]
    session = create(repo)
    result = client.get(f"/sessions/{session['id']}/messages?limit=200")
    assert result.status_code == 200
    assert result.json()["messages"] == [{"seq": 0, "role": "user", "content": "hello", "attachments": [], "createdAt": result.json()["messages"][0]["createdAt"]}]


@pytest.mark.parametrize("route,method", [("/me", "get"), ("/me/usage", "get"),
    ("/projects/{project}/files", "get"), ("/projects/{project}/changes", "get"),
    ("/projects/{project}", "delete"),
    ("/sessions/{session}", "patch")])
def test_http_missing_contracts_never_return_fake_success(env, client, route, method):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    session = create(repo)
    before = copy.deepcopy(db.data)
    result = getattr(client, method)(route.format(project=project["id"], session=session["id"]))
    assert result.status_code in {501, 503} and result.json()["error"]["code"]
    assert db.data == before


def test_http_frontend_session_contract_rejects_before_any_commit_native_opt_in_works(env, client):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    path = f"/projects/{project['id']}/sessions"
    before = copy.deepcopy(db.data)
    result = client.post(path, json={"prompt": "hello", "model": "fixture-model", "effort": "high"}, headers={"Idempotency-Key": "create"})
    assert result.status_code == 503 and result.json()["error"]["code"] == "session/client-contract-unavailable"
    assert db.data == before
    result = client.post(path + "?native=true", json={"prompt": "hello", "model": "fixture-model"}, headers={"Idempotency-Key": "create"})
    assert result.status_code == 201 and result.json()["status"] == "queued"
    assert "uid" not in result.json() and "event_sequence" not in result.json()
    assert result.json()["project_id"] == project["id"]
    assert client.get(path).status_code == 503
    assert client.get(path+"?native=true").json()["sessions"][0]["status"] == "queued"


@pytest.mark.parametrize("corruption", ["missing", "owner", "project", "missing_guard", "invalid_guard"])
def test_http_project_session_missing_binding_never_becomes_empty_success(env, client, corruption):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    session = project_session(repo, project)
    if corruption == "missing":
        del db.data[(SESSIONS_COLLECTION, session["id"])]
    elif corruption == "owner":
        db.data[(SESSIONS_COLLECTION, session["id"])]["uid"] = "mallory"
    elif corruption == "project":
        db.data[(SESSIONS_COLLECTION, session["id"])]["project_id"] = "another"
    elif corruption == "missing_guard":
        db.data[(PROJECTS_COLLECTION, project["id"])].pop("active_session_id")
    else:
        db.data[(PROJECTS_COLLECTION, project["id"])]["active_session_id"] = False
    result = client.get(f"/projects/{project['id']}/sessions?native=true")
    assert result.status_code == 503 and result.json()["error"]["code"] in {"project/session-history-unavailable", "project/session-guard-unavailable"}


def test_http_native_effort_and_attachments_fail_before_commit(env, client):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("alice", "project", {"name": "fixture"})
    before = copy.deepcopy(db.data)
    path = f"/projects/{project['id']}/sessions?native=true"
    result = client.post(path, json={"prompt": "hello", "model": "fixture-model", "effort": "high"}, headers={"Idempotency-Key": "one"})
    assert result.status_code == 503 and result.json()["error"]["code"] == "session/model-unavailable"
    result = client.post(path, json={"prompt": "hello", "model": "fixture-model", "attachments": [{"id": "foreign"}]}, headers={"Idempotency-Key": "one"})
    assert result.status_code == 501 and result.json()["error"]["code"] == "attachment/unavailable"
    assert db.data == before


def test_http_cancel_native_state_and_generic_update_preserve_authority(env, client):
    repo, db, _ = env
    session = create(repo)
    claim, _ = running(repo, session)
    before = copy.deepcopy(db.data)
    path = f"/sessions/{session['id']}"
    result = client.patch(path, json={"status": "idle", "recovery_required": False, "active_run_id": "forged"})
    assert result.status_code == 501 and db.data == before
    result = client.post(path+"/cancel", headers={"Idempotency-Key": "one"})
    assert result.status_code == 503 and db.data == before
    result = client.post(path+"/cancel?native=true", headers={"Idempotency-Key": "one"})
    assert result.status_code == 200 and result.json()["status"] == "cancel_requested"
    repo.finish(claim)
    assert client.post(path+"/cancel?native=true", headers={"Idempotency-Key": "one"}).json() == result.json()
    assert doc(db, SESSIONS_COLLECTION, session["id"])["status"] == "cancelled"


@pytest.mark.parametrize("suffix", ["", "/files", "/changes", "/sessions?limit=bad"])
def test_http_foreign_project_and_missing_project_are_opaque(env, client, suffix):
    repo, db, _ = env
    configure(db)
    project = repo.create_project("mallory", "project", {"name": "private"})
    foreign = client.get(f"/projects/{project['id']}"+suffix)
    missing = client.get("/projects/missing"+suffix)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()


def test_verified_email_uses_shared_revoked_token_verifier(monkeypatch):
    monkeypatch.setattr(auth, "_verify_token", lambda _: {"uid": "alice", "email_verified": False})
    with pytest.raises(Exception) as error:
        auth.get_verified_uid("Bearer fixture")
    assert error.value.status_code == 403
    monkeypatch.setattr(auth, "_verify_token", lambda _: {"uid": "alice", "email_verified": True})
    assert auth.get_verified_uid("Bearer fixture") == "alice"


def spawned_history(job, session):
    from app.execution.context import save
    for n in range(15):
        message = {"role": "assistant", "content": [{"type": "text", "text": f"original-{n}"}]}
        session["messages"].append(message)
        session["_transcript_append"] = [message]
        save(session)
        assert "_transcript_append" not in session
    from app.agent.compaction import COMPACTION_KEEP_LAST_N
    session["messages"] = [session["messages"][0], {"role": "user", "content": [{"type": "text", "text": "[earlier conversation summarized]\nfixture"}]}, *session["messages"][-COMPACTION_KEEP_LAST_N:]]
    session["_context_compacted"] = True
    save(session)
    assert "_context_compacted" not in session
    message = {"role": "assistant", "content": [{"type": "text", "text": "after compaction"}]}
    session["messages"].append(message)
    session["_transcript_append"] = [message]
    save(session)
    session["status"] = "idle"
    save(session)


def test_real_spawn_history_signals_are_consumed_without_false_recovery(env):
    from app.worker import process_claim
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    process_claim(repo, claim, spawned_history)
    current = doc(db, SESSIONS_COLLECTION, session["id"])
    assert current["status"] == "idle" and current["recovery_required"] is False
    assert repo.message_history(session["id"], "alice")["messages"][-1]["content"] == "after compaction"
    assert current["message_count"] == 17

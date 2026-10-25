"""Shared lifecycle/replay/transport contracts; no external service calls."""
import asyncio
import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware

import app.api.events as transport
import app.api.streaming as streaming
import app.auth as auth
import app.ratelimit as ratelimit
from app.execution.repository import EVENTS_COLLECTION, CommandError, LeaseLost
from app.store import SESSIONS_COLLECTION
from test_durable_runs import create, doc, env


def start(repo, session):
    claim = repo.claim(session["active_run_id"], "worker")
    _, state = repo.start(claim)
    return claim, state


def test_lifecycle_replay_is_ordered_atomic_and_has_no_transcript_payload(env):
    repo, db, clock = env
    session = create(repo)
    claim, current = start(repo, session)
    current["messages"].append({"role": "assistant", "content": [{"type": "text", "text": "private-synthetic-prompt"}]})
    current["_transcript_append"] = current["messages"][-1:]
    repo.save(claim, current)
    clock[0] -= 1  # Event IDs stay monotonic when the wall clock moves backward.
    current["status"] = "idle"
    repo.save(claim, current)
    repo.finish(claim)
    events = repo.event_history(session["id"], "alice")
    assert [event["type"] for event in events] == ["session.queued", "session.running", "session.updated", "session.idle"]
    assert [int(event["id"].split("-")[1]) for event in events] == [1, 2, 3, 4]
    assert len({event["id"].split("-")[0] for event in events}) == 1
    assert "private-synthetic-prompt" not in json.dumps(events)
    assert "uid" not in events[0] and "active_run_id" not in events[0]["data"]
    assert repo.event_history(session["id"], "alice", events[1]["id"]) == events[2:]
    assert repo.event_history(session["id"], "alice", events[-1]["id"]) == []


@pytest.mark.parametrize("counter", [None, 0, 9999])
def test_stale_worker_cannot_replace_or_regress_journal_metadata(env, counter):
    repo, db, _ = env
    session = create(repo)
    stale = copy.deepcopy(session)  # Same revision as start(), older event head.
    claim, _ = start(repo, session)
    original = copy.deepcopy(repo.event_history(session["id"], "alice"))
    stale.update(status="idle", event_time_ms=0)
    if counter is None:
        stale.pop("event_sequence")
    else:
        stale["event_sequence"] = counter
    repo.save(claim, stale)
    events = repo.event_history(session["id"], "alice")
    assert events[:2] == original
    assert events[-1]["id"].endswith("-3")


@pytest.mark.parametrize("field", ["uid", "id"])
def test_worker_cannot_change_session_identity_or_ownership(env, field):
    repo, db, _ = env
    session = create(repo)
    claim, current = start(repo, session)
    before = copy.deepcopy(db.data)
    current[field] = "different-identity"
    with pytest.raises(LeaseLost, match="identity"):
        repo.save(claim, current)
    assert db.data == before


def test_duplicate_commands_and_failed_commits_do_not_duplicate_or_partially_publish_events(env):
    repo, db, _ = env
    with ThreadPoolExecutor(4) as pool:
        sessions = list(pool.map(lambda _: create(repo), range(4)))
    session = sessions[0]
    assert len(repo.event_history(session["id"], "alice")) == 1
    # Simulate a collision/corruption: create-only journal writes must roll back
    # the corresponding session/job state instead of overwriting history.
    db.data[(EVENTS_COLLECTION, f"{session['id']}:2")] = {"immutable": True}
    claim = repo.claim(session["active_run_id"], "worker")
    before = copy.deepcopy(db.data)
    with pytest.raises(ValueError, match="already exists"):
        repo.start(claim)
    assert db.data == before


@pytest.mark.parametrize("missing_sequence", [1, 2])
def test_history_gaps_fail_closed_instead_of_advancing_cursor(env, missing_sequence):
    repo, db, _ = env
    session = create(repo)
    start(repo, session)
    del db.data[(EVENTS_COLLECTION, f"{session['id']}:{missing_sequence}")]
    with pytest.raises(CommandError, match="reconciliation"):
        repo.event_history(session["id"], "alice")


def test_bounded_replay_pages_and_cursor_scope(env):
    repo, _, _ = env
    session = create(repo)
    claim, current = start(repo, session)
    for _ in range(101):
        repo.save(claim, current)
    first = repo.event_history(session["id"], "alice")
    second = repo.event_history(session["id"], "alice", first[-1]["id"])
    assert len(first) == 100 and len(second) == 3
    assert first[-1]["id"] != second[0]["id"]
    for cursor in ("invalid", "0-1", "999-999999"):
        with pytest.raises(CommandError):
            repo.event_history(session["id"], "alice", cursor)
    with pytest.raises(CommandError) as missing:
        repo.event_history(session["id"], "mallory", "invalid")
    assert missing.value.status == 404


def test_cancellation_and_approval_states_are_not_translated_into_success(env):
    repo, _, _ = env
    session = create(repo)
    claim, current = start(repo, session)
    repo.cancel(session["id"], "alice")
    repo.finish(claim)
    events = repo.event_history(session["id"], "alice")
    assert [event["type"] for event in events][-2:] == ["session.cancel_requested", "session.cancelled"]
    assert events[-1]["data"]["status"] == "cancelled"
    before = len(events)
    repo.cancel(session["id"], "alice")
    assert len(repo.event_history(session["id"], "alice")) == before


class Connection:
    def __init__(self):
        self.disconnected = False

    async def is_disconnected(self):
        return self.disconnected


def test_stream_replays_new_durable_events_after_reconnect_without_control_ids(env, monkeypatch):
    repo, _, _ = env
    session = create(repo)
    first = repo.event_history(session["id"], "alice")[0]
    claim, current = start(repo, session)
    monkeypatch.setattr(streaming, "verify_identity", lambda _: auth.VerifiedIdentity("alice", time.time() + 3600))
    connection = Connection()

    async def consume():
        generator = transport.event_stream(connection, repo, session["id"], "alice", "Bearer synthetic", first["id"])
        frame = await anext(generator)
        assert "event: session.running" in frame and f"id: {first['id']}\n" not in frame
        current["status"] = "idle"
        repo.save(claim, current)
        assert await anext(generator) == ": keep-alive\n\n"
        assert "event: session.idle" in await anext(generator)
        connection.disconnected = True
        await generator.aclose()

    asyncio.run(consume())
    assert "id:" not in transport.closing("token-expired")


@pytest.mark.parametrize("expired", [False, True])
def test_stream_stops_before_replay_when_authorization_changes(env, monkeypatch, expired):
    repo, _, _ = env
    session = create(repo)

    def reject(_):
        raise auth.AuthenticationError("invalid token", expired=expired)

    monkeypatch.setattr(streaming, "verify_identity", reject)

    async def consume():
        return [frame async for frame in transport.event_stream(
            Connection(), repo, session["id"], "alice", "Bearer synthetic", None)]

    frames = asyncio.run(consume())
    assert frames == [transport.closing("token-expired" if expired else "authorization-failed")]


def test_stream_rechecks_tenant_before_replaying_the_next_page(env, monkeypatch):
    repo, db, _ = env
    session = create(repo)
    monkeypatch.setattr(streaming, "verify_identity", lambda _: auth.VerifiedIdentity("alice", time.time() + 3600))
    db.data[(SESSIONS_COLLECTION, session["id"])]["uid"] = "mallory"

    async def consume():
        return [frame async for frame in transport.event_stream(
            Connection(), repo, session["id"], "alice", "Bearer synthetic", None)]

    assert asyncio.run(consume()) == [transport.closing("authorization-failed")]


def test_stream_admission_is_bounded_and_release_is_idempotent():
    limiter = transport.StreamAdmission(total=2, per_user=1)
    release_a = limiter.acquire("alice")
    with pytest.raises(HTTPException) as per_user:
        limiter.acquire("alice")
    assert per_user.value.status_code == 429
    release_b = limiter.acquire("bob")
    with pytest.raises(HTTPException) as total:
        limiter.acquire("charlie")
    assert total.value.status_code == 503
    release_a()
    release_a()
    release_b()
    assert not limiter.counts


def test_slow_asgi_send_releases_capacity(env, monkeypatch):
    monkeypatch.setattr(streaming, "IO_TIMEOUT_SECONDS", 0.001)
    limiter = transport.StreamAdmission()

    async def content():
        yield ": keep-alive\n\n"

    async def run():
        response = transport.ManagedEventResponse(content(), limiter.acquire("alice"))

        async def blocked(*args):
            await asyncio.Future()

        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, blocked, blocked)

    asyncio.run(run())
    assert not limiter.counts


@pytest.fixture
def app_client(env, monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test-not-a-real-key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "test-not-a-real-secret")
    from app.main import app

    repo, _, _ = env
    monkeypatch.setattr(transport, "runs", repo)
    monkeypatch.setattr(ratelimit, "check_rate_limit", lambda *args: None)
    monkeypatch.setattr(auth, "ensure_initialized", lambda: None)
    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", lambda *args, **kwargs: {"uid": "alice", "exp": time.time() + 3600})
    with TestClient(app, raise_server_exceptions=False) as client:
        yield app, client


def test_http_owner_and_cursor_errors_use_current_client_envelope(app_client, env):
    app, client = app_client
    repo, _, _ = env
    session = create(repo)
    headers = {"Authorization": "Bearer synthetic", "Last-Event-ID": "wrong"}
    response = client.get(f"/sessions/{session['id']}/events", headers=headers)
    assert response.status_code == 422 and response.json()["error"]["code"] == "command/rejected"
    response = client.get("/sessions/missing/events", headers=headers)
    assert response.status_code == 404 and response.json()["detail"] == "session not found"
    assert client.get(f"/sessions/{session['id']}/events?token=synthetic").status_code == 401


def test_initial_expired_token_has_the_frontend_refresh_code(app_client, monkeypatch):
    _, client = app_client

    def expired(*args, **kwargs):
        raise auth.firebase_auth.ExpiredIdTokenError("synthetic expiration", ValueError("synthetic cause"))

    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", expired)
    response = client.get("/sessions/missing/events", headers={"Authorization": "Bearer synthetic"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "auth/token-expired"
    assert response.headers["www-authenticate"] == "Bearer"


def test_validation_error_envelope_does_not_echo_submitted_private_input(app_client):
    _, client = app_client
    response = client.post("/sessions", headers={"Authorization": "Bearer synthetic", "Idempotency-Key": "invalid-body"},
                           json={"task": {"private": "synthetic-private-request-input"}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request/invalid"
    assert isinstance(response.json()["detail"], list)
    assert "synthetic-private-request-input" not in response.text


def test_http_stream_headers_and_capacity_cleanup(app_client, env, monkeypatch):
    _, client = app_client
    session = create(env[0])

    def revoked(_):
        raise auth.AuthenticationError("invalid token")

    monkeypatch.setattr(streaming, "verify_identity", revoked)
    response = client.get(f"/sessions/{session['id']}/events", headers={"Authorization": "Bearer synthetic"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.text == transport.closing("authorization-failed")
    assert not transport.admission.counts


def test_storage_failure_closes_stream_without_private_diagnostics(env, monkeypatch):
    monkeypatch.setattr(streaming, "verify_identity", lambda _: auth.VerifiedIdentity("alice", time.time() + 3600))

    class Unavailable:
        def event_history(self, *args):
            raise RuntimeError("synthetic-private-database-diagnostic")

    async def consume():
        return [frame async for frame in transport.event_stream(
            Connection(), Unavailable(), "session", "alice", "Bearer synthetic", None)]

    assert asyncio.run(consume()) == [transport.closing("history-unavailable")]


def test_cross_origin_replay_preflight_allows_only_declared_headers(app_client):
    app, _ = app_client
    cors = next(layer for layer in app.user_middleware if layer.cls is CORSMiddleware)
    # Exercise the app's actual declared header/method/credential settings with
    # an explicit test origin; production origins remain unchanged.
    configured = CORSMiddleware(app, **{**cors.kwargs, "allow_origins": ["https://app.example.test"]})
    with TestClient(configured) as client:
        response = client.options("/sessions/example/events", headers={
            "Origin": "https://app.example.test", "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,last-event-id"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example.test"
    assert "last-event-id" in response.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers

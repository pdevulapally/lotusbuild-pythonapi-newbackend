"""Both stream transports share authorization leases and cleanup semantics."""
import asyncio
import copy
import json

import pytest

import app.api.events as events
import app.api.routes as routes
import app.api.streaming as streaming
from app.auth import AuthenticationError, VerifiedIdentity
from app.store import SESSIONS_COLLECTION
from test_durable_runs import create, env
from test_session_events import Connection, app_client


@pytest.fixture
def authorization(monkeypatch):
    state = {"wall": 100.0, "mono": 100.0, "expiry": 200.0, "revoked": False, "uid": "alice", "checks": 0}

    def verify(_):
        state["checks"] += 1
        if state["revoked"]:
            raise AuthenticationError("invalid token")
        return VerifiedIdentity(state["uid"], state["expiry"])

    monkeypatch.setattr(streaming, "verify_identity", verify)
    original = streaming.AuthorizationGate

    def gate(*args):
        return original(*args, clock=lambda: state["wall"], monotonic=lambda: state["mono"])

    monkeypatch.setattr(events, "AuthorizationGate", gate)
    monkeypatch.setattr(routes, "AuthorizationGate", gate)
    monkeypatch.setattr(events, "POLL_SECONDS", 0)
    monkeypatch.setattr(routes, "POLL_SECONDS", 0)
    return state


def owned_reader(env, monkeypatch):
    _, db, _ = env
    monkeypatch.setattr(routes, "get_session", lambda sid: copy.deepcopy(db.data.get((SESSIONS_COLLECTION, sid))))


@pytest.mark.parametrize("change", ["revoked", "expired", "owner"])
def test_snapshot_stream_stops_after_authority_or_owner_changes(env, monkeypatch, authorization, change):
    repo, db, _ = env
    session = create(repo)
    owned_reader(env, monkeypatch)

    async def consume():
        generator = routes.snapshot_stream(Connection(), session["id"], "alice", "Bearer synthetic")
        first = await anext(generator)
        assert first.startswith("data: ")
        payload = json.loads(first.removeprefix("data: "))
        assert "uid" not in payload and "system_prompt" not in payload
        authorization["mono"] += 2
        if change == "revoked":
            authorization["revoked"] = True
        elif change == "expired":
            authorization["wall"] = authorization["expiry"]
        else:
            db.data[(SESSIONS_COLLECTION, session["id"])]["uid"] = "mallory"
        result = await anext(generator)
        assert result.startswith("event: error\n") and "data: {\"id\"" not in result
        await generator.aclose()

    asyncio.run(consume())


def test_snapshot_detects_same_timestamp_status_change(env, monkeypatch, authorization):
    repo, db, _ = env
    session = create(repo)
    owned_reader(env, monkeypatch)

    async def consume():
        generator = routes.snapshot_stream(Connection(), session["id"], "alice", "Bearer synthetic")
        first = json.loads((await anext(generator)).removeprefix("data: "))
        db.data[(SESSIONS_COLLECTION, session["id"])]["status"] = "awaiting_approval"
        second = json.loads((await anext(generator)).removeprefix("data: "))
        assert first["updated_at"] == second["updated_at"] and len(first["messages"]) == len(second["messages"])
        assert second["status"] == "awaiting_approval"
        await generator.aclose()

    asyncio.run(consume())


@pytest.mark.parametrize("change", ["revoked", "expired", "owner"])
def test_authorization_is_checked_within_a_multi_event_page(env, authorization, change):
    repo, db, _ = env
    session = create(repo)
    claim = repo.claim(session["active_run_id"], "worker")
    repo.start(claim)

    async def consume():
        generator = events.event_stream(Connection(), repo, session["id"], "alice", "Bearer synthetic", None)
        assert "event: session.queued" in await anext(generator)
        authorization["mono"] += 2
        if change == "revoked":
            authorization["revoked"] = True
        elif change == "expired":
            authorization["wall"] = authorization["expiry"]
        else:
            db.data[(SESSIONS_COLLECTION, session["id"])]["uid"] = "mallory"
        result = await anext(generator)
        assert "event: stream.closing" in result and "id:" not in result
        assert "event: session.running" not in result
        await generator.aclose()

    asyncio.run(consume())


def test_expiry_during_ownership_read_is_checked_before_data_emission(authorization):
    def slow_owned_read():
        authorization["wall"] = authorization["expiry"]

    gate = streaming.AuthorizationGate("Bearer synthetic", "alice", slow_owned_read,
        clock=lambda: authorization["wall"], monotonic=lambda: authorization["mono"])
    with pytest.raises(AuthenticationError) as stopped:
        asyncio.run(gate.ensure())
    assert stopped.value.expired


def test_slow_check_cannot_grant_a_new_lease_after_its_authorization_window(authorization):
    def slow_owned_read():
        authorization["mono"] += streaming.AUTH_RECHECK_SECONDS + 0.1

    gate = streaming.AuthorizationGate("Bearer synthetic", "alice", slow_owned_read,
        clock=lambda: authorization["wall"], monotonic=lambda: authorization["mono"])
    with pytest.raises(AuthenticationError):
        asyncio.run(gate.ensure())


@pytest.mark.parametrize("asgi_version", ["2.3", "2.4"])
def test_blocked_body_send_closes_iterator_and_releases_slot(monkeypatch, asgi_version):
    monkeypatch.setattr(streaming, "IO_TIMEOUT_SECONDS", 0.001)
    limiter, closed, messages = streaming.StreamAdmission(), [], []

    async def content():
        try:
            yield "data: {}\n\n"
            pytest.fail("must not request another frame from a blocked stream")
        finally:
            closed.append(True)

    async def run():
        async def receive():
            await asyncio.Future()

        async def send(message):
            messages.append(message["type"])
            if message["type"] == "http.response.body":
                await asyncio.Future()

        response = streaming.ManagedEventResponse(content(), limiter.acquire("alice"))
        await response({"type": "http", "asgi": {"spec_version": asgi_version}}, receive, send)

    asyncio.run(run())
    assert messages == ["http.response.start", "http.response.body"]
    assert closed == [True] and not limiter.counts


def test_snapshot_and_events_share_capacity_and_owner_precedence(app_client, env, monkeypatch):
    _, client = app_client
    session = create(env[0])
    owned_reader(env, monkeypatch)
    monkeypatch.setattr(streaming.admission, "per_user", 1)
    release = streaming.admission.acquire("alice")
    try:
        for route in ("stream", "events"):
            response = client.get(f"/sessions/{session['id']}/{route}", headers={"Authorization": "Bearer synthetic"})
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "request/rejected"
            assert client.get(f"/sessions/missing/{route}", headers={"Authorization": "Bearer synthetic"}).status_code == 404
    finally:
        release()
    assert routes.admission is events.admission is streaming.admission

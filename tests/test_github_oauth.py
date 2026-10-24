"""Offline OAuth binding tests; fake transaction serialization is not emulator QA."""
import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth import get_uid
from app.github import oauth, routes


class Snapshot:
    def __init__(self, data):
        self.exists = data is not None
        self.data = copy.deepcopy(data)

    def to_dict(self):
        return self.data


class Ref:
    def __init__(self, db, key):
        self.db, self.key = db, key

    def create(self, data):
        assert self.key not in self.db.data
        self.db.data[self.key] = copy.deepcopy(data)

    def get(self, transaction=None):
        return Snapshot(self.db.data.get(self.key))


class MemoryDB:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def collection(self, name):
        assert name == oauth.TRANSACTIONS_COLLECTION
        return self

    def document(self, key):
        return Ref(self, key)

    def transaction(self):
        return self

    def update(self, ref, changes):
        self.data[ref.key].update(changes)


@pytest.fixture
def backend(monkeypatch):
    db = MemoryDB()
    monkeypatch.setattr(oauth, "db", db)
    original_consume = oauth._consume

    def serialized_consume(ref, secret):
        with db.lock:
            return original_consume(db.transaction(), ref, secret)

    monkeypatch.setattr(oauth, "_consume_transaction", serialized_consume)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_REDIRECT_URI", "https://api.example.test/auth/github/callback")
    monkeypatch.setenv("GITHUB_OAUTH_SUCCESS_REDIRECT_URL", "https://app.example.test/settings")
    return db


def state_from(url):
    return parse_qs(urlparse(url).query)["state"][0]


def test_state_is_browser_bound_single_use_and_opaque(backend):
    url, secret = oauth.begin_authorization("alice")
    state = state_from(url)
    assert "alice" not in state and secret not in url
    assert oauth.resolve_state(state, secret) == "alice"
    with pytest.raises(ValueError):
        oauth.resolve_state(state, secret)


@pytest.mark.parametrize("bad_secret", [None, "", "x" * 43])
def test_missing_or_wrong_browser_cannot_consume_state(backend, bad_secret):
    url, secret = oauth.begin_authorization("alice")
    state = state_from(url)
    with pytest.raises(ValueError):
        oauth.resolve_state(state, bad_secret)
    # A failed foreign-browser attempt must not destroy the owner's transaction.
    assert oauth.resolve_state(state, secret) == "alice"


def test_unknown_and_expired_state_rejected(backend, monkeypatch):
    with pytest.raises(ValueError):
        oauth.resolve_state("x" * 43, "y" * 43)
    monkeypatch.setattr(oauth.time, "time", lambda: 1000)
    url, secret = oauth.begin_authorization("alice")
    monkeypatch.setattr(oauth.time, "time", lambda: 1600)
    with pytest.raises(ValueError):
        oauth.resolve_state(state_from(url), secret)


def test_concurrent_callbacks_consume_only_once(backend):
    url, secret = oauth.begin_authorization("alice")

    def consume(_):
        try:
            return oauth.resolve_state(state_from(url), secret)
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(consume, range(2)))
    assert sorted(results) == ["alice", "rejected"]


@pytest.fixture
def client(backend, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_uid] = lambda: "alice"
    return TestClient(app, base_url="https://api.example.test", follow_redirects=False)


def test_start_sets_secure_cookie_and_callback_clears_it(client, monkeypatch):
    saved = []
    monkeypatch.setattr(oauth, "exchange_code_for_token", lambda code: "fake-provider-token")
    monkeypatch.setattr(routes, "save_token", lambda uid, token: saved.append((uid, token)))
    response = client.post("/auth/github/start")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert all(flag in cookie for flag in ["Secure", "HttpOnly", "SameSite=lax", "Path=/"])
    assert "Domain=" not in cookie
    state = state_from(response.json()["url"])
    response = client.get("/auth/github/callback", params={"code": "code", "state": state})
    assert response.status_code == 303
    assert saved == [("alice", "fake-provider-token")]
    assert "Max-Age=0" in response.headers["set-cookie"]


def test_foreign_browser_cannot_link_account(client, monkeypatch):
    monkeypatch.setattr(oauth, "exchange_code_for_token", lambda code: pytest.fail("must not exchange"))
    state = state_from(client.post("/auth/github/start").json()["url"])
    client.cookies.clear()
    response = client.get("/auth/github/callback", params={"code": "victim-code", "state": state})
    assert response.status_code == 400


def test_exchange_failure_consumes_state_requires_fresh_start(client, monkeypatch):
    calls = []

    def fail_exchange(code):
        calls.append(code)
        raise RuntimeError("sensitive provider response")

    monkeypatch.setattr(oauth, "exchange_code_for_token", fail_exchange)
    state = state_from(client.post("/auth/github/start").json()["url"])
    cookie = client.cookies.get(routes.BINDING_COOKIE)
    response = client.get("/auth/github/callback", params={"code": "code", "state": state})
    assert response.status_code == 502
    assert "sensitive" not in response.text
    client.cookies.set(routes.BINDING_COOKIE, cookie)
    response = client.get("/auth/github/callback", params={"code": "code", "state": state})
    assert response.status_code == 400
    assert calls == ["code"]
    assert state_from(client.post("/auth/github/start").json()["url"]) != state


def test_get_start_no_longer_creates_transaction(client):
    assert client.get("/auth/github/start").status_code == 405

"""HTTP and log disclosure regressions; all external services are replaced."""
import json
from types import SimpleNamespace

import pytest
import redis
import stripe
from fastapi.testclient import TestClient

import app.auth as auth
import app.api.routes as routes
import app.ratelimit as ratelimit


PRIVATE_DIAGNOSTIC = "synthetic-private-provider-diagnostic"


def fail_verification(*args, **kwargs):
    pytest.fail("malformed authentication must not initialize Firebase or verify a token")


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "test-not-a-real-key")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "test-not-a-real-secret")
    from app.main import app

    monkeypatch.setattr(ratelimit, "check_rate_limit", lambda *args: None)
    monkeypatch.setattr(auth, "ensure_initialized", lambda: None)
    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", lambda *args, **kwargs: {"uid": "alice"})
    with TestClient(app, raise_server_exceptions=False) as connection:
        yield connection


@pytest.mark.parametrize("authorization", [None, "", "Basic synthetic-token", "Bearer", "Bearer "])
def test_missing_or_malformed_bearer_is_401_before_firebase(client, monkeypatch, caplog, authorization):
    monkeypatch.setattr(auth, "ensure_initialized", fail_verification)
    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", fail_verification)
    headers = {} if authorization is None else {"Authorization": authorization}
    response = client.get("/memory", headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "synthetic-token" not in response.text + caplog.text


def test_invalid_token_does_not_echo_sdk_diagnostics(client, monkeypatch, caplog):
    calls = []

    def reject(token, *, check_revoked):
        calls.append((token, check_revoked))
        raise ValueError(PRIVATE_DIAGNOSTIC)

    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", reject)
    response = client.get("/memory", headers={"Authorization": "Bearer synthetic-id-token"})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid token"
    assert response.json()["error"]["code"] == "auth/invalid-token"
    assert response.headers["www-authenticate"] == "Bearer"
    assert calls == [("synthetic-id-token", True)]
    assert PRIVATE_DIAGNOSTIC not in response.text + caplog.text
    assert "synthetic-id-token" not in caplog.text
    assert any(json.loads(record.message)["event"] == "auth_failure" for record in caplog.records)


@pytest.mark.parametrize("decoded", [{}, {"uid": ""}, {"uid": None}, {"uid": 123}, None])
def test_unusable_identity_fails_closed(client, monkeypatch, decoded):
    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", lambda *args, **kwargs: decoded)
    monkeypatch.setattr(routes, "get_memory", lambda *args: pytest.fail("no authorized identity"))
    response = client.get("/memory", headers={"Authorization": "Bearer synthetic-id-token"})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid token"


def test_valid_identity_reaches_owned_handler_with_revocation_check(client, monkeypatch):
    calls = []

    def verify(token, *, check_revoked):
        calls.append((token, check_revoked))
        return {"uid": "alice"}

    monkeypatch.setattr(auth.firebase_auth, "verify_id_token", verify)
    monkeypatch.setattr(routes, "get_memory", lambda uid: [uid])
    response = client.get("/memory", headers={"Authorization": "Bearer synthetic-id-token"})
    assert response.status_code == 200
    assert response.json() == {"notes": ["alice"]}
    assert calls == [("synthetic-id-token", True)]


@pytest.mark.parametrize("failure", [ValueError(PRIVATE_DIAGNOSTIC), stripe.SignatureVerificationError(PRIVATE_DIAGNOSTIC, "synthetic-signature")])
def test_invalid_webhook_does_not_echo_signature_or_payload(client, monkeypatch, caplog, failure):
    import app.billing.routes as billing

    def reject(*args):
        raise failure

    monkeypatch.setattr(billing.stripe.Webhook, "construct_event", reject)
    monkeypatch.setattr(billing, "run_idempotent", lambda *args: pytest.fail("invalid webhook cannot mutate"))
    response = client.post("/billing/webhook", content=PRIVATE_DIAGNOSTIC,
                           headers={"Stripe-Signature": "synthetic-signature"})
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid webhook signature"
    assert PRIVATE_DIAGNOSTIC not in response.text + caplog.text
    assert "synthetic-signature" not in response.text + caplog.text


def test_valid_webhook_still_uses_verified_event_receipt(client, monkeypatch):
    import app.billing.routes as billing

    monkeypatch.setattr(billing.stripe.Webhook, "construct_event", lambda *args: {"id": "evt_test", "type": "unhandled.test"})
    calls = []

    def receipt(uid, scope, key, do):
        calls.append((uid, scope, key))
        status, body = do()
        assert status == 200
        return SimpleNamespace(body=body)

    monkeypatch.setattr(billing, "run_idempotent", receipt)
    response = client.post("/billing/webhook", content="synthetic-payload")
    assert response.status_code == 200
    assert response.json() == {"received": True}
    assert calls == [(None, "webhook", "evt_test")]


def test_unhandled_error_logs_category_without_exception_or_path(client, monkeypatch, caplog):
    def broken_handler(session_id):
        raise RuntimeError(PRIVATE_DIAGNOSTIC)

    monkeypatch.setattr(routes, "get_session", broken_handler)
    response = client.get("/sessions/synthetic-private-path", headers={"Authorization": "Bearer synthetic-id-token"})
    assert response.status_code == 500
    assert response.json()["detail"] == "internal server error"
    assert PRIVATE_DIAGNOSTIC not in response.text + caplog.text
    assert "synthetic-private-path" not in caplog.text
    event = json.loads(next(record.message for record in caplog.records if record.name == "security"))
    assert event == {"event": "unhandled_exception", "error_type": "RuntimeError"}


def test_rate_limit_outage_keeps_existing_behavior_without_private_diagnostics(monkeypatch, caplog):
    def unavailable():
        raise redis.ConnectionError(PRIVATE_DIAGNOSTIC)

    monkeypatch.setattr(ratelimit, "_redis", SimpleNamespace(pipeline=unavailable))
    ratelimit.check_rate_limit("anon", "synthetic-identity")
    assert PRIVATE_DIAGNOSTIC not in caplog.text
    assert "synthetic-identity" not in caplog.text
    event = json.loads(next(record.message for record in caplog.records if record.name == "security"))
    assert event == {"event": "rate_limit_backend_unavailable", "bucket": "anon", "error_type": "ConnectionError"}

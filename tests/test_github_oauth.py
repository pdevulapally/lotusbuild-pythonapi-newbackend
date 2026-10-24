import os
import time
from urllib.parse import unquote

from cryptography.fernet import Fernet

os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client-id")
os.environ.setdefault("GITHUB_REDIRECT_URI", "https://example.com/callback")

from app.github import oauth  # noqa: E402 - needs the env vars set above first


def _extract_state(url: str) -> str:
    return unquote(url.split("state=")[1].split("&")[0])


def test_build_and_resolve_state_round_trip():
    url = oauth.build_authorize_url("uid123")
    assert "client_id=test-client-id" in url
    assert oauth.resolve_state(_extract_state(url)) == "uid123"


def test_tampered_state_is_rejected():
    state = _extract_state(oauth.build_authorize_url("uid123"))
    tampered = state[:-1] + ("A" if state[-1] != "A" else "B")
    try:
        oauth.resolve_state(tampered)
        assert False, "expected an error on a tampered state"
    except Exception:
        pass


def test_expired_state_is_rejected():
    state = _extract_state(oauth.build_authorize_url("uid123"))
    original_ttl = oauth.STATE_TTL_SECONDS
    oauth.STATE_TTL_SECONDS = 0
    try:
        time.sleep(1.1)
        try:
            oauth.resolve_state(state)
            assert False, "expected an error on an expired state"
        except Exception:
            pass
    finally:
        oauth.STATE_TTL_SECONDS = original_ttl


if __name__ == "__main__":
    test_build_and_resolve_state_round_trip()
    test_tampered_state_is_rejected()
    test_expired_state_is_rejected()
    print("ok")

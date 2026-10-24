import json
import os
import urllib.parse
import urllib.request

from app.github.credentials import _fernet

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
STATE_TTL_SECONDS = 600


def build_authorize_url(uid: str) -> str:
    # The OAuth `state` param IS the CSRF token: a Fernet encryption of the uid,
    # which embeds its own timestamp and is verified (incl. a TTL) on the way
    # back in resolve_state - no separate stateful one-time-token store needed.
    state = _fernet().encrypt(uid.encode()).decode()
    params = {
        "client_id": os.environ["GITHUB_CLIENT_ID"],
        "redirect_uri": os.environ["GITHUB_REDIRECT_URI"],
        "scope": "repo",
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def resolve_state(state: str) -> str:
    """Raises on a tampered or expired state - the only validation this CSRF
    mechanism needs, since the token is self-contained."""
    return _fernet().decrypt(state.encode(), ttl=STATE_TTL_SECONDS).decode()


def exchange_code_for_token(code: str) -> str:
    payload = urllib.parse.urlencode(
        {
            "client_id": os.environ["GITHUB_CLIENT_ID"],
            "client_secret": os.environ["GITHUB_CLIENT_SECRET"],
            "code": code,
            "redirect_uri": os.environ["GITHUB_REDIRECT_URI"],
        }
    ).encode()
    request = urllib.request.Request(
        TOKEN_URL, data=payload, headers={"Accept": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request) as response:
        data = json.loads(response.read())
    if "access_token" not in data:
        raise RuntimeError(f"GitHub token exchange failed: {data}")
    return data["access_token"]

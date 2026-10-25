"""Synthetic import/liveness smoke of extracted bytes; NOT production readiness."""
import os
from pathlib import Path
import socket
import sys
import threading

root = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root))
for name in ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_SERVICE_ACCOUNT_JSON", "FIREBASE_SERVICE_ACCOUNT_FILE"):
    os.environ.pop(name, None)
for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "E2B_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
    os.environ[name] = "test-not-a-real-key"
os.environ.pop("ENABLE_EXPERIMENTAL_WORKER", None)
original_connect, original_pair = socket.socket.connect, socket.socketpair
state = threading.local()
def no_network(*args, **kwargs):
    raise AssertionError("external Python socket prohibited during artifact smoke")
def connect(sock, address):
    return original_connect(sock, address) if getattr(state, "pair", False) else no_network()
def socketpair(*args, **kwargs):
    state.pair = True
    try:
        return original_pair(*args, **kwargs)
    finally:
        state.pair = False
socket.create_connection, socket.socket.connect, socket.socket.connect_ex = no_network, connect, no_network
socket.socketpair = socketpair

from fastapi.testclient import TestClient
import app.main
import app.ratelimit
assert Path(app.main.__file__).resolve().is_relative_to(root), "smoke imported the checkout instead of extracted release"
# Explicit synthetic adapter: this is liveness/import smoke, not Redis readiness.
app.ratelimit.check_rate_limit = lambda *args, **kwargs: None
with TestClient(app.main.app) as client:
    response = client.get("/health")
    assert response.status_code == 200 and response.json() == {"status": "ok"}
print("Extracted application import and synthetic liveness passed; deployment remains blocked")

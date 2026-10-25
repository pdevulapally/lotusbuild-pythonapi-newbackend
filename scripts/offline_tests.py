"""Run the local suite with synthetic keys and Python socket dispatch blocked."""
import os
from pathlib import Path
import socket
import sys
import threading

import pytest


for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "E2B_API_KEY",
             "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"):
    os.environ[name] = "test-not-a-real-key"


def no_connection(*args, **kwargs):
    raise AssertionError("network dispatch prohibited during offline verification")


original_connect = socket.socket.connect
original_socketpair = socket.socketpair
socketpair_context = threading.local()


def guarded_connect(sock, address):
    # Windows implements asyncio's wake-up pipe with a loopback socket pair.
    # Permit only that stdlib setup on its calling thread, not other clients.
    if getattr(socketpair_context, "active", False):
        return original_connect(sock, address)
    return no_connection()


def local_socketpair(*args, **kwargs):
    socketpair_context.active = True
    try:
        return original_socketpair(*args, **kwargs)
    finally:
        socketpair_context.active = False


socket.create_connection = no_connection
socket.socket.connect = guarded_connect
socket.socket.connect_ex = no_connection
socket.socketpair = local_socketpair
os.chdir(Path(__file__).resolve().parents[1])
sys.path.insert(0, os.getcwd())
if __name__ == "__main__":
    raise SystemExit(pytest.main(["-q", "--tb=short", *sys.argv[1:]]))

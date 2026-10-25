"""Single numeric loopback contract shared by CI and synthetic SDK subprocesses."""
import re

PROJECT = "demo-lotusbuild-local"


def require_endpoint(value):
    match = re.fullmatch(r"127\.0\.0\.1:([0-9]{1,5})", value) if isinstance(value, str) else None
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise ValueError("explicit numeric IPv4 loopback emulator required")
    return value

import os
import re

_DEFAULT_RISKY_BASH_PATTERNS = ",".join(
    [
        r"rm\s+-rf",
        r"\bsudo\b",
        r"chmod\s+777",
        r"\|\s*sh\b",
        r"\|\s*bash\b",
        r"git\s+push\s+.*--force",
        r">\s*/etc/",
        r"\bmkfs\b",
        r"\bdd\s+if=",
    ]
)

RISKY_BASH_PATTERNS = os.environ.get("RISKY_BASH_PATTERNS", _DEFAULT_RISKY_BASH_PATTERNS).split(",")

# One line per tool: "always" risky, a list of regexes to check against the
# tool's `command` input, or absent (never risky). Env-overridable data for the
# one tool that needs a pattern list - same registry convention as everywhere
# else in this codebase (sandbox/manager.py's _BACKENDS, providers' _ADAPTER_CLASSES).
RISK_POLICY: dict[str, str | list[str]] = {
    "expose_port": "always",
    "create_pull_request": "always",
    "bash": RISKY_BASH_PATTERNS,
}


def is_risky(name: str, input: dict) -> bool:
    policy = RISK_POLICY.get(name)
    if policy is None:
        return False
    if policy == "always":
        return True
    command = input.get("command", "")
    return any(re.search(pattern, command) for pattern in policy)

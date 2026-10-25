"""Local/CI evidence checks; never imports application providers or deploys."""
import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET

from scripts.emulator_endpoint import PROJECT, require_endpoint

ROOT = Path(__file__).resolve().parents[1]
EMULATOR_VERSION = "1.21.0"
EMULATOR_SHA256 = "c3d3680a89d946a90a027365ea14c26c6472a162bcf37f099bbb1ebd66d25e8e"
KEYS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "E2B_API_KEY", "STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET")


def junit(path, suite, system):
    tree = ET.parse(path).getroot()
    cases = list(tree.iter("testcase"))
    if not cases or list(tree.iter("failure")) or list(tree.iter("error")):
        raise ValueError("missing or failed test evidence")
    allowed = set(json.loads((ROOT / "ci/platform-skips.json").read_text())[system])
    passed, skips, nodes = [], [], set()
    for case in cases:
        node = case.get("classname", "") + "::" + case.get("name", "")
        if node in nodes:
            raise ValueError("duplicate test evidence: " + node)
        nodes.add(node)
        integration = node.startswith("tests.integration.test_firestore_execution::")
        skipped = case.find("skipped") is not None
        if suite == "emulator" and not integration:
            raise ValueError("unexpected emulator test")
        if skipped:
            if not (suite == "offline" and integration) and node not in allowed:
                raise ValueError("unexpected skipped test: " + node)
            skips.append(node)
        else:
            passed.append(node)
    minimum = (80 if system == "windows" else 79) if suite == "emulator" else (737 if system == "windows" else 730)
    if len(passed) < minimum:
        raise ValueError("insufficient required suite evidence")
    critical = ({"tests.integration.test_firestore_execution::test_same_command_race_commits_one_receipt_job_and_slot",
                 "tests.integration.test_firestore_execution::test_task_dispatch_revoke_race_never_reauthorizes_or_double_consumes"}
                if suite == "emulator" else {
                    "tests.test_process_supervision::test_spawned_completion_commits_only_after_clean_exit[success]",
                    "tests.test_task_authorization::test_cross_owner_scope_and_unsupported_firebase_namespace_fail_before_reader",
                    "tests.test_task_authorization::test_turn_ceiling_and_model_reservations_are_cumulative",
                    "tests.test_release_pipeline::test_callback_content_drift_never_claims_forward_or_rollback_health[start]",
                    "tests.test_release_pipeline::test_interruption_journal_cannot_automatically_replay[old_moved]"})
    if not critical <= set(passed):
        raise ValueError("required critical test evidence missing")
    return {"passed": len(passed), "skipped": skips, "junit_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "linux_descendant_containment_qualified": False}


def validate_workflow(path):
    import yaml
    value = yaml.load(Path(path).read_text(), Loader=yaml.BaseLoader)
    if set(value["on"]) != {"push", "pull_request", "workflow_dispatch"} or value["permissions"] != {"contents": "read"}:
        raise ValueError("unsafe workflow event/permission")
    text = Path(path).read_text()
    if any(word in text for word in ("secrets.", "id-token:", "pull_request_target", "workflow_run:", "self-hosted")):
        raise ValueError("credential or privileged workflow path")
    for job in value["jobs"].values():
        if "timeout-minutes" not in job:
            raise ValueError("unbounded CI job")
        for step in job["steps"]:
            if "uses" in step and not re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", step["uses"]):
                raise ValueError("unpinned or unapproved action")
            if step.get("uses", "").startswith("actions/checkout@") and step.get("with", {}).get("persist-credentials") != "false":
                raise ValueError("checkout persists credentials")
    return value


def static():
    validate_workflow(ROOT / ".github/workflows/ci.yml")
    for folder in ("app", "scripts", "tests"):
        for path in (ROOT / folder).rglob("*.py"):
            ast.parse(path.read_bytes(), filename=str(path))
    if (ROOT / "requirements.txt").read_bytes() != (ROOT / "requirements/runtime.in").read_bytes():
        raise ValueError("runtime requirements changed without lock input review")
    for name in ("runtime", "ci"):
        data = (ROOT / f"requirements/{name}.lock").read_text()
        # No editable/VCS/URL/index overrides or unhashed resolver fallbacks.
        if any(t in data for t in ("https://", "http://", "git+", "--index", "-e ")):
            raise ValueError("unreviewed dependency source")
        entries = re.split(r"\n(?=[a-zA-Z0-9])", data.strip())
        if any(not re.match(r"[A-Za-z0-9_.-]+==[^\s]+", e) or not re.search(r"--hash=sha256:[0-9a-f]{64}", e) for e in entries):
            raise ValueError("dependency is not exact and hashed")


def secret_scan():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    patterns = [re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
                re.compile(rb"(?:sk_live_|whsec_)[A-Za-z0-9]{20,}")]
    for name in result.stdout.decode().split("\x00"):
        if not name:
            continue
        path = Path(name)
        if (path.name.startswith(".env") and name != ".env.example") or path.suffix.lower() in {".pem", ".p12"} or "service-account" in name.lower():
            raise ValueError("credential-shaped tracked file: " + name)
        content = (ROOT / path).read_bytes()
        if any(p.search(content) for p in patterns):
            raise ValueError("credential-shaped tracked content: " + name)


def emulator(jar, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    jar = Path(jar)
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://storage.googleapis.com/firebase-preview-drop/emulator/cloud-firestore-emulator-v{EMULATOR_VERSION}.jar"
        with urllib.request.urlopen(url, timeout=60) as source, jar.open("xb") as destination:
            shutil.copyfileobj(source, destination)
    if hashlib.sha256(jar.read_bytes()).hexdigest() != EMULATOR_SHA256:
        raise ValueError("emulator checksum mismatch")
    java = shutil.which("java")
    if not java:
        raise ValueError("Java runtime unavailable")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    endpoint = require_endpoint(f"127.0.0.1:{port}")
    log = (output / "emulator.log").open("wb")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    child = subprocess.Popen([java, "-jar", str(jar), "--host=127.0.0.1", f"--port={port}",
        "--project_id=" + PROJECT, "--single_project_mode=true", "--single_project_mode_error=true"], stdout=log, stderr=subprocess.STDOUT, **options)
    def terminate(_signal, _frame):
        raise KeyboardInterrupt("CI emulator interrupted")
    previous_term = signal.signal(signal.SIGTERM, terminate)
    try:
        deadline = time.monotonic() + 30
        while True:
            if child.poll() is not None:
                raise ValueError("owned emulator exited before ready")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("emulator startup timeout")
                time.sleep(.1)
        env = os.environ.copy()
        for name in ("GOOGLE_APPLICATION_CREDENTIALS", "FIREBASE_SERVICE_ACCOUNT_JSON", "FIREBASE_SERVICE_ACCOUNT_FILE"):
            env.pop(name, None)
        env.update({name: "test-not-a-real-key" for name in KEYS})
        env.update(FIRESTORE_EMULATOR_HOST=endpoint, PYTHONDONTWRITEBYTECODE="1")
        path = output / "emulator.xml"
        subprocess.run([sys.executable, "-m", "pytest", "tests/integration/test_firestore_execution.py", "-q", "-o", "junit_family=legacy",
                        "--basetemp=" + str(output / "pytest-tmp"), "-o", "cache_dir=" + str(output / "cache"),
                        "--junitxml=" + str(path)], cwd=ROOT, env=env, check=True, timeout=900)
        evidence = junit(path, "emulator", "windows" if os.name == "nt" else "linux")
        (output / "emulator-evidence.json").write_text(json.dumps(evidence, indent=2))
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        log.close()
        with socket.socket() as check:
            check.bind(("127.0.0.1", port))
        (output / "emulator-cleanup.json").write_text(json.dumps({"pid": child.pid, "exited": child.poll() is not None, "port_bindable": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("static")
    commands.add_parser("secrets")
    report = commands.add_parser("junit")
    report.add_argument("--path", required=True)
    report.add_argument("--suite", choices=("offline", "emulator"), required=True)
    report.add_argument("--system", choices=("windows", "linux"), required=True)
    em = commands.add_parser("emulator")
    em.add_argument("--jar", required=True)
    em.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "static":
        static()
    elif args.command == "secrets":
        secret_scan()
    elif args.command == "junit":
        print(json.dumps(junit(args.path, args.suite, args.system)))
    else:
        emulator(args.jar, args.output)


if __name__ == "__main__":
    main()

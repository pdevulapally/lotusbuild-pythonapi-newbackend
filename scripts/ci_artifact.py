"""Build a checked source+wheel bundle; do not import or deploy the application."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

from scripts.ci_checks import junit
from scripts.release import build, extract, runtime, validate


def create(root, evidence_dir, output, revision):
    root, evidence_dir, output = Path(root), Path(evidence_dir), Path(output)
    if platform.system() != "Linux" or platform.python_version() != "3.14.6":
        raise ValueError("release job requires the exact Linux/Python build runtime")
    # Re-validate downloaded evidence, never trust a green job name or JSON bool.
    proof = {"offline": junit(evidence_dir / "offline.xml", "offline", "linux"),
             "emulator": junit(evidence_dir / "emulator.xml", "emulator", "linux"),
             "revision": revision, "github_run_id": os.environ.get("GITHUB_RUN_ID"),
             "image_version": os.environ.get("ImageVersion"),
             "workflow_sha256": hashlib.sha256((root / ".github/workflows/ci.yml").read_bytes()).hexdigest(),
             "runtime_lock_sha256": hashlib.sha256((root / "requirements/runtime.lock").read_bytes()).hexdigest(),
             "ci_lock_sha256": hashlib.sha256((root / "requirements/ci.lock").read_bytes()).hexdigest(),
             "production_ready": False}
    output.mkdir(parents=True, exist_ok=True)
    wheels = output / "wheelhouse"
    wheels.mkdir()
    subprocess.run([sys.executable, "-m", "pip", "--isolated", "download", "--only-binary=:all:", "--require-hashes",
                    "--index-url", "https://pypi.org/simple", "--dest", str(wheels), "-r", str(root / "requirements/runtime.lock")], check=True)
    archive = output / "release.tar.gz"
    checksum = build(root, wheels, archive, revision, proof)
    validate(archive, checksum, revision, expected_runtime=runtime())
    destination = output / "extracted"
    destination.mkdir()
    extract(archive, checksum, revision, destination, expected_runtime=runtime())
    metadata = {"revision": revision, "sha256": checksum, "runtime": runtime(), "production_ready": False, "evidence": proof}
    (output / "provenance.json").write_text(json.dumps(metadata, indent=2))
    (output / "release.sha256").write_text(checksum + "\n")
    print(json.dumps(metadata))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("root", "evidence", "output", "revision"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    create(args.root, args.evidence, args.output, args.revision)

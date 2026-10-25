"""Local synthetic release bytes and temporary promotion directories only."""
import io
import json
import os
from pathlib import Path
import tarfile
import xml.etree.ElementTree as ET

import pytest

from scripts import release
from scripts.ci_checks import junit, validate_workflow
from scripts.local_promotion import sandbox, Interrupted

REVISION = "a" * 40


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "source"
    for folder in ("app", "requirements", "deploy"):
        (root / folder).mkdir(parents=True)
    (root / "app/main.py").write_text("# synthetic release, never executed\n")
    wheel = b"synthetic wheel bytes, never installed"
    (root / "requirements/runtime.lock").write_text("fixture==1 --hash=sha256:" + release.digest(wheel))
    (root / "deploy/release-policy.json").write_text(json.dumps({"production_release_supported": False, "recovery_protocol": 1, "unresolved_gates": ["synthetic"]}))
    (root / "firestore.indexes.json").write_text("{}")
    (root / "firestore.rules").write_text("// synthetic")
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "fixture-1-py3-none-any.whl").write_bytes(wheel)
    archive = tmp_path / "release.tar.gz"
    checksum = release.build(root, wheels, archive, REVISION, {"synthetic": True})
    return root, wheels, archive, checksum


def rewrite(archive, mutate):
    with tarfile.open(archive, "r:gz") as source:
        entries = [(m, source.extractfile(m).read()) for m in source]
    with tarfile.open(archive, "w:gz") as target:
        for member, content in mutate(entries):
            target.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return release.digest(archive.read_bytes())


def test_exact_bundle_determinism_and_empty_extraction(bundle, tmp_path):
    root, wheels, archive, checksum = bundle
    second = tmp_path / "again.tar.gz"
    assert release.build(root, wheels, second, REVISION, {"synthetic": True}) == checksum
    destination = tmp_path / "candidate"
    destination.mkdir()
    metadata = release.extract(archive, checksum, REVISION, destination, expected_runtime=release.runtime())
    assert metadata["production_ready"] is False
    assert (destination / "app/main.py").read_bytes() == (root / "app/main.py").read_bytes()
    with pytest.raises(release.ReleaseError):
        release.extract(archive, checksum, REVISION, destination)


@pytest.mark.parametrize("name", ["../outside", "/outside", "app\\escape.py", "C:/escape", "app/CON.py", "app/a./b.py", "app/../main.py"])
def test_unsafe_paths_rejected_before_any_extraction(bundle, tmp_path, name):
    _, _, archive, _ = bundle
    def changed(entries):
        member = tarfile.TarInfo(name)
        member.size = 1
        return entries + [(member, b"x")]
    checksum = rewrite(archive, changed)
    destination = tmp_path / "empty"
    destination.mkdir()
    with pytest.raises(release.ReleaseError):
        release.extract(archive, checksum, REVISION, destination)
    assert not list(destination.iterdir())


@pytest.mark.parametrize("attack", ["duplicate", "casefold", "symlink", "hardlink", "device", "file-parent", "payload", "protocol", "metadata-list"])
def test_hostile_member_or_manifest_never_partially_extracts(bundle, tmp_path, attack):
    _, _, archive, _ = bundle
    def changed(entries):
        member = tarfile.TarInfo("app/extra.py")
        content = b"x"
        member.size = 1
        if attack in {"duplicate", "casefold"}:
            member.name = "app/main.py" if attack == "duplicate" else "APP/MAIN.PY"
        elif attack in {"symlink", "hardlink", "device"}:
            member.type = {"symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE, "device": tarfile.CHRTYPE}[attack]
            member.linkname = "../../outside"
        elif attack == "file-parent":
            member.name = "app/main.py/child.py"
        elif attack in {"protocol", "metadata-list"}:
            result = []
            for m, data in entries:
                if m.name == "release.json":
                    record = json.loads(data)
                    record["recovery_protocol"] = 99
                    data = release.canonical([] if attack == "metadata-list" else record)
                    m.size = len(data)
                result.append((m, data))
            return result
        elif attack == "payload":
            return [(m, b"x" * len(data) if m.name == "app/main.py" else data) for m, data in entries]
        return entries + [(member, content)]
    checksum = rewrite(archive, changed)
    destination = tmp_path / "empty"
    destination.mkdir()
    with pytest.raises(release.ReleaseError):
        release.extract(archive, checksum, REVISION, destination)
    assert not list(destination.iterdir())


def test_checksum_revision_runtime_and_production_gate(bundle, monkeypatch):
    _, _, archive, checksum = bundle
    for digest, revision, runtime in [("0" * 64, REVISION, None), (checksum, "b" * 40, None), (checksum, REVISION, {"fake": True})]:
        with pytest.raises(release.ReleaseError):
            release.validate(archive, digest, revision, expected_runtime=runtime)
    monkeypatch.setenv("ENABLE_EXPERIMENTAL_WORKER", "1")
    monkeypatch.setenv("DEPLOY_ENABLED", "1")
    with pytest.raises(release.ReleaseError, match="unsupported"):
        release.require_production({"production_release_supported": True})


def test_local_promotion_preserves_previous_and_holds_lock(tmp_path):
    with sandbox(tmp_path) as area:
        area.fixture("current", b"old")
        area.fixture("candidate", b"new")
        def stop():
            assert (area.root / "promotion.lock").exists()
            return True
        assert area.promote(stop, lambda: True, lambda: True)["phase"] == "healthy"
        assert (area.root / "previous/payload").read_bytes() == b"old"
        assert (area.root / "current/payload").read_bytes() == b"new"
        with pytest.raises(Interrupted):
            area.promote(stop, lambda: True, lambda: True)


def test_health_failure_rolls_back_only_exact_previous(tmp_path):
    with sandbox(tmp_path) as area:
        area.fixture("current", b"old")
        area.fixture("candidate", b"new")
        checks = iter([False, True])
        assert area.promote(lambda: True, lambda: True, lambda: next(checks))["phase"] == "rollback_healthy"
        assert (area.root / "current/payload").read_bytes() == b"old"
        assert (area.root / "candidate/payload").read_bytes() == b"new"


@pytest.mark.parametrize("phase", ["validated", "stopped", "old_moved", "published", "health_failed", "rollback_stopped", "failed_moved", "rolled_back"])
def test_interruption_journal_cannot_automatically_replay(tmp_path, phase):
    with sandbox(tmp_path) as area:
        area.fixture("current", b"old")
        area.fixture("candidate", b"new")
        def fault(current):
            if current == phase:
                raise KeyboardInterrupt("synthetic interruption")
        with pytest.raises(KeyboardInterrupt):
            area.promote(lambda: True, lambda: True, lambda: False, interrupt=fault)
        calls = []
        with pytest.raises(Interrupted):
            area.promote(lambda: calls.append(True), lambda: True, lambda: True)
        assert not calls


@pytest.mark.parametrize("fault", ["lock", "journal", "stop", "protocol", "drift", "rollback-start"])
def test_promotion_uncertainty_cannot_publish_or_claim_health(tmp_path, fault):
    with sandbox(tmp_path) as area:
        area.fixture("current", b"old")
        area.fixture("candidate", b"new", protocol=2 if fault == "protocol" else 1)
        if fault == "lock":
            (area.root / "promotion.lock").write_text("other-owner")
        if fault == "journal":
            (area.root / "promotion.json").write_text("malformed previous journal")
        def stop():
            if fault == "drift":
                (area.root / "candidate/payload").write_bytes(b"changed")
            return None if fault == "stop" else True
        starts = iter([True, False])
        with pytest.raises((release.ReleaseError, FileExistsError)):
            area.promote(stop, lambda: next(starts), lambda: False)


def test_workflow_rejects_privileged_or_unpinned_execution(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = (root / ".github/workflows/ci.yml").read_text()
    validate_workflow(root / ".github/workflows/ci.yml")
    for bad in [source.replace("pull_request:", "pull_request_target:"), source.replace("@11d5960a326750d5838078e36cf38b85af677262", "@main"), source.replace("contents: read", "contents: write")]:
        path = tmp_path / "workflow.yml"
        path.write_text(bad)
        with pytest.raises(ValueError):
            validate_workflow(path)


def test_junit_does_not_hide_skips_or_failure(tmp_path):
    for tag in ("skipped", "failure", "error"):
        tree = ET.Element("testsuite")
        case = ET.SubElement(tree, "testcase", classname="tests.test_release_pipeline", name="required")
        ET.SubElement(case, tag)
        path = tmp_path / "result.xml"
        ET.ElementTree(tree).write(path)
        with pytest.raises(ValueError):
            junit(path, "offline", "windows")


@pytest.mark.parametrize("phase", ["start", "health", "rolled_back", "rollback_health"])
def test_callback_content_drift_never_claims_forward_or_rollback_health(tmp_path, phase):
    with sandbox(tmp_path) as area:
        area.fixture("current", b"old")
        area.fixture("candidate", b"new")
        calls = [0, 0]
        def mutate():
            (area.root / "current/payload").write_bytes(b"unexpected replacement")
        def start():
            calls[0] += 1
            if phase == "start" and calls[0] == 1:
                mutate()
            return True
        def healthy():
            calls[1] += 1
            if phase == "health" or (phase == "rollback_health" and calls[1] == 2):
                mutate()
                return True
            return phase not in {"rolled_back", "rollback_health"}
        def interrupt(current):
            if phase == "rolled_back" and current == phase:
                mutate()
        with pytest.raises(release.ReleaseError):
            area.promote(lambda: True, start, healthy, interrupt=interrupt)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction extraction boundary")
def test_extraction_rejects_actual_windows_junction_target_and_ancestor(bundle, tmp_path):
    import subprocess
    _, _, archive, checksum = bundle
    outside = tmp_path / "synthetic-outside"
    outside.mkdir()
    junction = tmp_path / "junction"
    result = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(junction), str(outside)], capture_output=True)
    assert result.returncode == 0
    for target in (junction, junction / "candidate"):
        if target.name == "candidate":
            target.mkdir()
        with pytest.raises(release.ReleaseError):
            release.extract(archive, checksum, REVISION, target)
    assert sorted(p.name for p in outside.iterdir()) == ["candidate"]
    assert not list((outside / "candidate").iterdir())


def test_archive_must_be_a_regular_file(tmp_path):
    with pytest.raises(release.ReleaseError):
        release.validate(tmp_path, "0" * 64, REVISION)


def test_duplicate_junit_cannot_inflate_a_suite(tmp_path):
    tree = ET.Element("testsuite")
    for _ in range(800):
        ET.SubElement(tree, "testcase", classname="tests.fake", name="same")
    path = tmp_path / "duplicate.xml"
    ET.ElementTree(tree).write(path)
    with pytest.raises(ValueError, match="duplicate"):
        junit(path, "offline", "windows")


@pytest.mark.parametrize("value", [None, "localhost:8787", "0.0.0.0:8787", "127.0.0.1:0", "127.0.0.1:65536", "https://127.0.0.1:8787", "[::1]:8787"])
def test_shared_emulator_endpoint_never_selects_a_remote_or_implicit_target(value):
    from scripts.emulator_endpoint import require_endpoint
    with pytest.raises(ValueError):
        require_endpoint(value)


def test_shared_endpoint_accepts_the_harness_selected_port(monkeypatch):
    from scripts.emulator_endpoint import require_endpoint
    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "127.0.0.1:49123")
    assert require_endpoint(os.environ["FIRESTORE_EMULATOR_HOST"]) == "127.0.0.1:49123"

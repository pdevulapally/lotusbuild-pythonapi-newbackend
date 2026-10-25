"""Stopped-service promotion simulation in self-owned temporary directories only.

Not a server adapter: callbacks are synthetic test functions; no shell/SSH/systemd.
Two renames are NOT an atomic exchange. An unfinished journal never auto-replays.
File journal fsync is process-interruption evidence; directory/power-loss
durability and hostile concurrent local filesystem writers are not qualified.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import uuid

from scripts.release import ReleaseError, canonical, digest, RECOVERY_PROTOCOL


class Interrupted(ReleaseError):
    pass


@contextmanager
def sandbox(parent=None):
    with tempfile.TemporaryDirectory(prefix="lotus-release-simulation-", dir=parent) as directory:
        area = Sandbox(Path(directory), uuid.uuid4().hex)
        (area.root / ".owned").write_text(area.token)
        yield area


class Sandbox:
    def __init__(self, root, token):
        self.root, self.token = root, token

    def _check(self):
        if self.root.is_symlink() or (self.root / ".owned").read_text() != self.token:
            raise ReleaseError("local simulation ownership changed")

    def fixture(self, name, content, protocol=RECOVERY_PROTOCOL):
        self._check()
        if name not in {"current", "candidate"}:
            raise ReleaseError("simulation fixture name rejected")
        path = self.root / name
        path.mkdir()
        (path / "payload").write_bytes(content)
        (path / "release.json").write_bytes(canonical({"digest": digest(content), "recovery_protocol": protocol}))
        return path

    def _identity(self, name):
        self._check()
        path = self.root / name
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) or not path.is_dir():
            raise ReleaseError("simulation directory changed")
        if {p.name for p in path.iterdir()} != {"payload", "release.json"}:
            raise ReleaseError("unexpected simulation content")
        for child in path.iterdir():
            if child.is_symlink() or not child.is_file():
                raise ReleaseError("linked simulation content")
        record = json.loads((path / "release.json").read_bytes())
        if record != {"digest": digest((path / "payload").read_bytes()), "recovery_protocol": RECOVERY_PROTOCOL}:
            raise ReleaseError("simulation content or recovery protocol changed")
        return record

    def promote(self, stop, start, healthy, *, interrupt=lambda phase: None):
        """All callbacks must explicitly return True. No callback recovery replay."""
        self._check()
        lock = self.root / "promotion.lock"
        owner = self.token.encode()
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(descriptor, owner)
            os.fsync(descriptor)
            journal = self.root / "promotion.json"
            if journal.exists() or journal.is_symlink():
                raise Interrupted("previous journal requires explicit inspection")
            old, new = self._identity("current"), self._identity("candidate")
            if (self.root / "previous").exists():
                raise ReleaseError("previous directory already exists")
            state = {"schema": 1, "old": old, "new": new, "phase": "validated"}
            def record(phase):
                state["phase"] = phase
                temporary = self.root / "journal.next"
                with temporary.open("xb") as stream:
                    stream.write(canonical(state))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, journal)
                interrupt(phase)
            record("validated")
            # Callbacks cannot silently change bytes/paths during stop validation.
            if stop() is not True:
                record("stop_unconfirmed")
                raise Interrupted("stop unconfirmed; no publication")
            if self._identity("current") != old or self._identity("candidate") != new:
                raise Interrupted("content changed while stopping")
            record("stopped")
            os.rename(self.root / "current", self.root / "previous")
            record("old_moved")
            os.rename(self.root / "candidate", self.root / "current")
            record("published")
            if self._identity("current") != new:
                raise Interrupted("published identity changed")
            if start() is True and healthy() is True:
                if self._identity("current") != new or self._identity("previous") != old:
                    raise Interrupted("healthy callback changed release identity")
                record("healthy")
                return state
            record("health_failed")
            if stop() is not True:
                record("rollback_stop_unconfirmed")
                raise Interrupted("rollback stop unconfirmed")
            if self._identity("current") != new or self._identity("previous") != old:
                raise Interrupted("rollback identity changed")
            record("rollback_stopped")
            os.rename(self.root / "current", self.root / "candidate")
            record("failed_moved")
            os.rename(self.root / "previous", self.root / "current")
            record("rolled_back")
            if self._identity("current") != old or self._identity("candidate") != new:
                raise Interrupted("restored release identity changed")
            if start() is not True or healthy() is not True:
                record("rollback_health_unconfirmed")
                raise Interrupted("rollback health unconfirmed")
            if self._identity("current") != old or self._identity("candidate") != new:
                raise Interrupted("rollback callback changed release identity")
            record("rollback_healthy")
            return state
        finally:
            os.close(descriptor)
            # Never remove another owner's lock, and never delete the journal.
            if lock.is_file() and not lock.is_symlink() and lock.read_bytes() == owner:
                lock.unlink()


if __name__ == "__main__":
    with sandbox() as area:
        area.fixture("current", b"synthetic previous release")
        area.fixture("candidate", b"synthetic candidate release")
        result = area.promote(lambda: True, lambda: True, lambda: True)
        print(json.dumps({"simulation_only": True, "phase": result["phase"]}))

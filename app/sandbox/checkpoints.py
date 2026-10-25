"""Bounded, uncompressed workspace checkpoint format and trusted adapter boundary.

Production has no configured adapter. LocalHarness below handles only private,
test-owned files; it is deliberately never used by SandboxManager or the worker.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Protocol
import uuid

from app.execution.workspaces import identity, MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_MANIFEST_BYTES


class CheckpointInvalid(ValueError):
    pass


class PersistenceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Limits:
    files: int = MAX_FILES
    file_bytes: int = MAX_FILE_BYTES
    total_bytes: int = MAX_TOTAL_BYTES
    manifest_bytes: int = MAX_MANIFEST_BYTES

    def __post_init__(self):
        ceilings = (MAX_FILES, MAX_FILE_BYTES, MAX_TOTAL_BYTES, MAX_MANIFEST_BYTES)
        if any(type(v) is not int or not 1 <= v <= ceiling for v, ceiling in
               zip((self.files, self.file_bytes, self.total_bytes, self.manifest_bytes), ceilings)):
            raise CheckpointInvalid("invalid technical checkpoint limits")


def digest(data):
    return hashlib.sha256(data).hexdigest()


_EXCLUDED = {".git", ".ssh", ".aws", ".azure", ".gcloud", ".config", ".codex", ".agents",
             "node_modules", ".venv", "__pycache__", "credentials", "credentials.json",
             "service-account.json", "id_rsa", "id_ed25519", ".npmrc", ".pypirc", ".netrc"}
_DEVICE = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?\Z", re.I)
_HASH = re.compile(r"[0-9a-f]{64}\Z")


def safe_path(value):
    if type(value) is not str or not value or len(value.encode("utf-8")) > 240:
        raise CheckpointInvalid("invalid workspace path")
    parts = value.split("/")
    if len(parts) > 16 or any(not p or p in {".", ".."} or p[-1:] in {".", " "}
                            or _DEVICE.fullmatch(p) or any(ord(c) < 32 or ord(c) > 126 or c in '\\:<>"|?*' for c in p)
                            for p in parts):
        raise CheckpointInvalid("unsafe workspace path")
    return parts


def excluded(path):
    parts = safe_path(path)
    return any(p.lower() in _EXCLUDED or p.lower().startswith(".env") or
               p.lower().endswith((".pem", ".key", ".p12", ".pfx")) for p in parts)


def _content_allowed(data):
    # Defense in depth, not a claim to recognize arbitrary credentials embedded
    # in source. Production needs an approved selection/content policy.
    if b"PRIVATE KEY-----" in data or re.search(rb"(?i)(?:api[_-]?key|secret|password|access[_-]?token)['\"]?\s*[:=]\s*['\"]?[^\s'\"]{8,}", data):
        raise CheckpointInvalid("potential secret content excluded")


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def validate_manifest(raw, receipt, limits):
    if type(raw) is not bytes or len(raw) > limits.manifest_bytes or digest(raw) != receipt["manifest_digest"]:
        raise CheckpointInvalid("manifest integrity failure")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise CheckpointInvalid("invalid manifest encoding") from None
    keys = {"version", "uid", "session_id", "checkpoint_id", "run_id", "generation", "expires_at", "files"}
    if type(value) is not dict or set(value) != keys or _encode(value) != raw:
        raise CheckpointInvalid("manifest is not canonical")
    if type(value["version"]) is not int or value["version"] != 1:
        raise CheckpointInvalid("unsupported manifest version")
    for field, key in (("uid", "uid"), ("session_id", "session_id"), ("checkpoint_id", "id"),
                       ("run_id", "run_id"), ("generation", "generation"), ("expires_at", "expires_at")):
        if type(value[field]) is not type(receipt[key]) or value[field] != receipt[key]:
            raise CheckpointInvalid("manifest ownership or version mismatch")
    files = value["files"]
    if type(files) is not list or len(files) > limits.files or len(files) != receipt["file_count"]:
        raise CheckpointInvalid("file count exceeds checkpoint bounds")
    seen, total, previous = set(), 0, ""
    for item in files:
        if type(item) is not dict or set(item) != {"path", "bytes", "sha256"}:
            raise CheckpointInvalid("invalid file entry")
        path = item["path"]
        safe_path(path)
        if excluded(path) or path.lower() in seen or path <= previous:
            raise CheckpointInvalid("excluded, duplicate or unordered file path")
        # Reject file/directory collisions on case-insensitive filesystems.
        lowered = path.lower()
        if any(lowered.startswith(p + "/") or p.startswith(lowered + "/") for p in seen):
            raise CheckpointInvalid("file/directory collision")
        seen.add(lowered)
        previous = path
        size = item["bytes"]
        if (type(size) is not int or not 0 <= size <= limits.file_bytes or
                type(item["sha256"]) is not str or not _HASH.fullmatch(item["sha256"])):
            raise CheckpointInvalid("invalid file size or hash")
        total += size
        if total > limits.total_bytes:
            raise CheckpointInvalid("workspace exceeds byte limit")
    if total != receipt["total_bytes"]:
        raise CheckpointInvalid("workspace size mismatch")
    return value


class ObjectStore(Protocol):
    """Private immutable objects. Implementations must bound I/O before allocation.

    put is create-only, exact-byte idempotent and durable before returning.
    Keys are opaque hashes; clients never supply arbitrary storage paths/URLs.
    """
    def put(self, key: str, data: bytes) -> None: ...
    def read(self, key: str, maximum: int) -> bytes: ...


class WorkspaceAdapter(Protocol):
    """Trusted quiesced export; no links, races, external writers or unbounded reads.

    materialize must allocate a fresh private candidate, write regular files only,
    fsync before a completion marker, and never overwrite/adopt a live workspace.
    Failed candidates are quarantined. No archive extraction or command execution.
    """
    def capture(self, source: str, limits: Limits): ...
    def materialize(self, restore_id: str, files, checkpoint): ...
    def verify(self, restore_id: str, files, checkpoint): ...


class WorkspacePersistence:
    def __init__(self, repository, store: ObjectStore = None, adapter: WorkspaceAdapter = None, *, limits=None):
        if store is None or adapter is None:
            raise PersistenceUnavailable("workspace persistence requires verified storage and workspace adapters")
        self.repo, self.store, self.adapter = repository, store, adapter
        self.limits = limits or Limits()

    @staticmethod
    def _key(receipt, suffix):
        return identity([receipt["uid"], receipt["session_id"], receipt["id"], suffix])

    def _manifest(self, receipt):
        raw = self.store.read(self._key(receipt, "manifest"), self.limits.manifest_bytes)
        return validate_manifest(raw, receipt, self.limits)

    def snapshot(self, claim, *, key, source, expires_at):
        receipt = self.repo.begin_workspace_checkpoint(claim, key=key, expires_at=expires_at, source_digest=identity(source))
        if receipt["state"] == "ready":
            manifest = self._manifest(receipt)
            self._files(receipt, manifest, lambda: self.repo.checkpoint(claim))
            return receipt
        files, total = [], 0
        # No incomplete metadata is restorable. A crash leaves only immutable
        # unreferenced objects and a staging receipt/lock for reconciliation.
        for path, data in self.adapter.capture(source, self.limits):
            self.repo.checkpoint(claim)
            safe_path(path)
            if excluded(path) or type(data) is not bytes:
                raise CheckpointInvalid("excluded path or invalid content")
            _content_allowed(data)
            total += len(data)
            if len(files) >= self.limits.files or len(data) > self.limits.file_bytes or total > self.limits.total_bytes:
                raise CheckpointInvalid("workspace exceeds checkpoint bounds")
            sha = digest(data)
            files.append({"path": path, "bytes": len(data), "sha256": sha})
            self.store.put(self._key(receipt, sha), data)
        files.sort(key=lambda f: f["path"])
        manifest = {"version": 1, "uid": receipt["uid"], "session_id": receipt["session_id"],
                    "checkpoint_id": receipt["id"], "run_id": receipt["run_id"],
                    "generation": receipt["generation"], "expires_at": expires_at, "files": files}
        raw = _encode(manifest)
        sealed = {**receipt, "manifest_digest": digest(raw), "file_count": len(files), "total_bytes": total}
        validate_manifest(raw, sealed, self.limits)
        self.repo.checkpoint(claim)
        self.store.put(self._key(receipt, "manifest"), raw)
        # Verify stored bytes, not only writer return values, before publication.
        verified = self._manifest(sealed)
        self._files(sealed, verified, lambda: self.repo.checkpoint(claim))
        return self.repo.publish_workspace_checkpoint(claim, checkpoint_id=receipt["id"],
                    manifest_digest=sealed["manifest_digest"], file_count=len(files), total_bytes=total)

    def _files(self, receipt, manifest, checkpoint=lambda: None):
        result = []
        for item in manifest["files"]:
            checkpoint()
            data = self.store.read(self._key(receipt, item["sha256"]), item["bytes"])
            if len(data) != item["bytes"] or digest(data) != item["sha256"]:
                raise CheckpointInvalid("file integrity failure")
            _content_allowed(data)
            result.append((item["path"], data))
        return result

    def restore(self, claim, *, checkpoint_id, key):
        receipt = self.repo.begin_workspace_restore(claim, checkpoint_id=checkpoint_id, key=key)
        # Reuse owner-scoped transactional reads; no alternate auth/storage state.
        source = self.repo.workspace_restore_source(claim, checkpoint_id=checkpoint_id)
        manifest = self._manifest(source)
        check = lambda: self.repo.checkpoint(claim)
        files = self._files(source, manifest, check)
        if receipt["state"] == "materialized":
            self.adapter.verify(receipt["id"], files, check)
            return receipt
        self.adapter.materialize(receipt["id"], files, check)
        self.adapter.verify(receipt["id"], files, check)
        return self.repo.complete_workspace_restore(claim, restore_id=receipt["id"])


def _regular(path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400 or
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)) or
            not directory and info.st_nlink != 1):
        raise CheckpointInvalid("links and special files are forbidden")
    return info


class LocalHarness:
    """Deterministic local test adapter, not an E2B or production file adapter.

    root must be a fresh private test directory owned by this task. Only fixture
    files generated under it can be captured. No arbitrary user path is accepted.
    External writers are outside this harness's trust contract; production must
    supply handle-relative no-follow export and provider quiescence/isolation.
    """
    def __init__(self, root):
        self.root = Path(root)
        # Exclusive allocation prevents accidentally attaching existing data.
        self.root.mkdir(parents=False, exist_ok=False)
        self._paths()
        for p in (self.objects, self.fixtures, self.candidates):
            p.mkdir()
        marker = self.root / ".local-fixture-store-v1"
        with marker.open("xb") as stream:
            stream.write(b"local-test-only; no provider adoption\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _paths(self):
        self.objects, self.fixtures, self.candidates = (self.root / p for p in ("objects", "fixtures", "quarantine"))

    @classmethod
    def reopen(cls, root):
        """Reopen only this task's previously created private test root."""
        result = object.__new__(cls)
        result.root = Path(root)
        _regular(result.root, directory=True)
        result._paths()
        for path in (result.objects, result.fixtures, result.candidates):
            _regular(path, directory=True)
        marker = result.root / ".local-fixture-store-v1"
        if _regular(marker).st_size != len(b"local-test-only; no provider adoption\n") or marker.read_bytes() != b"local-test-only; no provider adoption\n":
            raise CheckpointInvalid("not a local fixture store")
        return result

    def fixture(self, files):
        name = uuid.uuid4().hex
        root = self.fixtures / name
        root.mkdir()
        for path, data in files.items():
            safe_path(path)
            target = root.joinpath(*path.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return name

    def _under(self, root, parts):
        _regular(self.root, directory=True)
        _regular(root, directory=True)
        current = root
        for part in parts[:-1]:
            current /= part
            _regular(current, directory=True)
        return root.joinpath(*parts)

    def capture(self, source, limits):
        if type(source) is not str or not re.fullmatch(r"[0-9a-f]{32}", source):
            raise CheckpointInvalid("only task-owned local fixture sources are supported")
        root = self._under(self.fixtures, [source])
        _regular(root, directory=True)
        visited = 0
        def walk(directory, prefix="", depth=0):
            nonlocal visited
            _regular(directory, directory=True)
            if depth > 16:
                raise CheckpointInvalid("workspace tree depth exceeds bound")
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > limits.files * 4:
                        raise CheckpointInvalid("workspace traversal exceeds bound")
                    path = prefix + entry.name
                    safe_path(path)
                    if excluded(path):
                        continue
                    target = Path(entry.path)
                    info = target.lstat()
                    if stat.S_ISDIR(info.st_mode):
                        _regular(target, directory=True)
                        yield from walk(target, path + "/", depth + 1)
                    else:
                        target = self._under(root, safe_path(path))
                        before = _regular(target)
                        if before.st_size > limits.file_bytes:
                            raise CheckpointInvalid("file exceeds bound")
                        with target.open("rb") as stream:
                            opened = os.fstat(stream.fileno())
                            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                                raise CheckpointInvalid("file identity changed")
                            data = stream.read(limits.file_bytes + 1)
                            after = os.fstat(stream.fileno())
                        if (len(data) != before.st_size or before.st_mtime_ns != after.st_mtime_ns or
                                before.st_size != after.st_size):
                            raise CheckpointInvalid("file changed during checkpoint")
                        yield path, data
        yield from walk(root)

    def _object(self, key):
        if type(key) is not str or not _HASH.fullmatch(key):
            raise CheckpointInvalid("invalid opaque object key")
        return self._under(self.objects, [key])

    def put(self, key, data):
        target = self._object(key)
        temporary = self.objects / (uuid.uuid4().hex + ".partial")
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Atomic create-only publication; never replace an existing object.
            os.link(temporary, target)
        except FileExistsError:
            if self.read(key, len(data)) != data:
                raise CheckpointInvalid("immutable object conflicts") from None
        finally:
            temporary.unlink()

    def read(self, key, maximum):
        target = self._object(key)
        before = _regular(target)
        if before.st_size > maximum:
            raise CheckpointInvalid("stored object exceeds bound")
        with target.open("rb") as stream:
            if os.fstat(stream.fileno()).st_ino != before.st_ino:
                raise CheckpointInvalid("stored object identity changed")
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise CheckpointInvalid("stored object exceeds bound")
        return data

    def materialize(self, restore_id, files, checkpoint):
        if type(restore_id) is not str or not _HASH.fullmatch(restore_id):
            raise CheckpointInvalid("invalid restore identity")
        root = self._under(self.candidates, [restore_id])
        root.mkdir(exist_ok=False)
        for path, data in files:
            checkpoint()
            target = root.joinpath(*safe_path(path))
            target.parent.mkdir(parents=True, exist_ok=True)
            target = self._under(root, safe_path(path))
            with target.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        checkpoint()
        # Marker lives outside user files, so no reserved path can collide.
        marker = self.candidates / (restore_id + ".complete")
        with marker.open("xb") as stream:
            stream.write(_encode([(p, digest(b)) for p, b in files]))
            stream.flush()
            os.fsync(stream.fileno())

    def verify(self, restore_id, files, checkpoint):
        if type(restore_id) is not str or not _HASH.fullmatch(restore_id):
            raise CheckpointInvalid("invalid restore identity")
        root = self._under(self.candidates, [restore_id])
        _regular(root, directory=True)
        marker = self._under(self.candidates, [restore_id + ".complete"])
        expected = _encode([(p, digest(b)) for p, b in files])
        _regular(marker)
        with marker.open("rb") as stream:
            if stream.read(len(expected) + 1) != expected:
                raise CheckpointInvalid("restore completion marker invalid")
        expected_paths = {p for p, _ in files}
        expected_dirs = {"/".join(p.split("/")[:i]) for p in expected_paths for i in range(1, len(p.split("/")))}
        seen = set()
        def walk(directory, prefix=""):
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = prefix + entry.name
                    if path not in expected_paths and path not in expected_dirs:
                        raise CheckpointInvalid("unmanifested restore entry")
                    target = Path(entry.path)
                    if path in expected_dirs:
                        _regular(target, directory=True)
                        walk(target, path + "/")
                    else:
                        _regular(target)
                        seen.add(path)
        walk(root)
        if seen != expected_paths:
            raise CheckpointInvalid("restore tree is incomplete")
        for path, data in files:
            checkpoint()
            target = self._under(root, safe_path(path))
            info = _regular(target)
            if info.st_size != len(data):
                raise CheckpointInvalid("restored file size changed")
            with target.open("rb") as stream:
                if stream.read(len(data) + 1) != data:
                    raise CheckpointInvalid("restored file integrity failure")

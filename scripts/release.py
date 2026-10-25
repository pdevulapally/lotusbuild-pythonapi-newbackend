"""Credential-free immutable bundle validation. No deployment adapter is enabled."""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import platform
import re
import sys
import tarfile

SCHEMA = 1
RECOVERY_PROTOCOL = 1
PYTHON = "3.14.6"
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_FILES = 10000
MAX_FILE = 100 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
FIXED = {"requirements/runtime.lock", "deploy/release-policy.json", "firestore.indexes.json", "firestore.rules"}


class ReleaseError(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def safe_name(name):
    if not isinstance(name, str):
        raise ReleaseError("unsafe release path")
    parts = name.split("/")
    if (not name or len(name) > 240 or
            any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts) or
            any(c in name for c in "\\:\x00") or PurePosixPath(name).is_absolute() or
            any(not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or
                re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", p.split(".")[0].upper()) for p in parts)):
        raise ReleaseError("unsafe release path")
    return name


def allowed(name):
    return (name in FIXED or name == "release.json" or
            (name.startswith("app/") and name.endswith(".py")) or
            (name.startswith("wheelhouse/") and name.count("/") == 1 and name.endswith(".whl")))


def runtime():
    return {"system": platform.system().lower(), "machine": platform.machine().lower(),
            "python": platform.python_version(), "cache_tag": sys.implementation.cache_tag,
            "libc": list(platform.libc_ver())}


def require_production(*_args, **_kwargs):
    # Intentionally source-level: changing ENV, policy JSON or CLI cannot enable CD.
    raise ReleaseError("production promotion unsupported: release prerequisites remain unresolved")


def build(root, wheelhouse, output, revision, evidence):
    root, wheelhouse, output = Path(root).resolve(), Path(wheelhouse).resolve(), Path(output)
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ReleaseError("invalid revision")
    files = {}
    def source_bytes(path):
        if not path.resolve().is_relative_to(root) or any(
                p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
                for p in (path, *path.parents) if p.is_relative_to(root)):
            raise ReleaseError("linked source")
        return path.read_bytes()
    for path in sorted((root / "app").rglob("*.py")):
        files[path.relative_to(root).as_posix()] = source_bytes(path)
    for name in sorted(FIXED):
        path = root / name
        files[name] = source_bytes(path)
    lock = files["requirements/runtime.lock"].decode()
    wheels = list(wheelhouse.iterdir())
    if not wheels or any(not p.is_file() or p.is_symlink() or p.suffix != ".whl" for p in wheels):
        raise ReleaseError("reviewed wheelhouse required")
    for path in sorted(wheels):
        content = path.read_bytes()
        if "sha256:" + digest(content) not in lock:
            raise ReleaseError("wheel is not authorized by runtime lock")
        files["wheelhouse/" + path.name] = content
    metadata = {"schema": SCHEMA, "revision": revision, "recovery_protocol": RECOVERY_PROTOCOL,
                "runtime": runtime(), "production_ready": False, "evidence": evidence,
                "files": {name: {"sha256": digest(data), "bytes": len(data)} for name, data in sorted(files.items())}}
    files["release.json"] = canonical(metadata)
    if len(files) > MAX_FILES or sum(map(len, files.values())) > MAX_TOTAL:
        raise ReleaseError("release exceeds size bound")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive output creation never overwrites a previous artifact.
    with output.open("xb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as zipped:
        with tarfile.open(fileobj=zipped, mode="w") as archive:
            for name, data in sorted(files.items()):
                safe_name(name)
                if not allowed(name) or len(data) > MAX_FILE:
                    raise ReleaseError("unexpected or oversized release file")
                item = tarfile.TarInfo(name)
                item.size, item.mode, item.mtime = len(data), 0o600, 0
                archive.addfile(item, io.BytesIO(data))
    return digest(output.read_bytes())


def validate(archive_path, expected_digest, revision, *, expected_runtime=None):
    if not re.fullmatch(r"[0-9a-f]{64}", expected_digest) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ReleaseError("invalid release identity")
    path = Path(archive_path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ARCHIVE:
        raise ReleaseError("archive type or size rejected")
    # Same file descriptor for hash, manifest validation and extraction source bytes.
    with path.open("rb") as source:
        content = source.read(MAX_ARCHIVE + 1)
    if digest(content) != expected_digest or len(content) > MAX_ARCHIVE:
        raise ReleaseError("release checksum mismatch")
    files, folded, total = {}, set(), 0
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for member in archive:
            name = safe_name(member.name)
            total += member.size
            if (not member.isfile() or member.size < 0 or member.size > MAX_FILE or total > MAX_TOTAL or
                    len(files) >= MAX_FILES or not allowed(name) or name.casefold() in folded):
                raise ReleaseError("unexpected, duplicate or oversized release entry")
            folded.add(name.casefold())
            files[name] = archive.extractfile(member).read(MAX_FILE + 1)
        if "release.json" not in files or len(files["release.json"]) > 2 * 1024 * 1024:
            raise ReleaseError("release metadata unavailable")
    for name in files:
        if any("/".join(name.casefold().split("/")[:n]) in folded for n in range(1, len(name.split("/")))):
            raise ReleaseError("release file is also a parent path")
    metadata = json.loads(files.pop("release.json"))
    if (not isinstance(metadata, dict) or set(metadata) != {"schema", "revision", "recovery_protocol", "runtime", "production_ready", "evidence", "files"} or
            type(metadata["schema"]) is not int or metadata["schema"] != SCHEMA or metadata["revision"] != revision or
            type(metadata["recovery_protocol"]) is not int or metadata["recovery_protocol"] != RECOVERY_PROTOCOL or
            metadata["production_ready"] is not False or not isinstance(metadata["evidence"], dict) or
            metadata["files"] != {name: {"sha256": digest(data), "bytes": len(data)} for name, data in sorted(files.items())} or
            not FIXED <= set(files) or "app/main.py" not in files or not any(n.startswith("wheelhouse/") for n in files)):
        raise ReleaseError("release manifest, content or protocol mismatch")
    policy = json.loads(files["deploy/release-policy.json"])
    if policy.get("production_release_supported") is not False or policy.get("recovery_protocol") != RECOVERY_PROTOCOL or not policy.get("unresolved_gates"):
        raise ReleaseError("release safety policy mismatch")
    lock = files["requirements/runtime.lock"].decode()
    if any("sha256:" + digest(data) not in lock for name, data in files.items() if name.startswith("wheelhouse/")):
        raise ReleaseError("wheel hash not present in lock")
    if expected_runtime is not None and metadata["runtime"] != expected_runtime:
        raise ReleaseError("release runtime mismatch")
    return metadata, files


def extract(archive, checksum, revision, destination, *, expected_runtime=None):
    metadata, files = validate(archive, checksum, revision, expected_runtime=expected_runtime)
    target = Path(destination).absolute()
    if (any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in (target, *target.parents)) or
            not target.is_dir() or any(target.iterdir())):
        raise ReleaseError("new empty real extraction directory required")
    for name, data in files.items():
        path = target.joinpath(*name.split("/"))
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
    (target / "release.json").write_bytes(canonical(metadata))
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build")
    for name in ("root", "wheelhouse", "output", "revision", "evidence"):
        build_cmd.add_argument("--" + name, required=True)
    check = commands.add_parser("check")
    for name in ("archive", "sha256", "revision"):
        check.add_argument("--" + name, required=True)
    check.add_argument("--extract")
    commands.add_parser("promote")
    args = parser.parse_args()
    if args.command == "promote":
        require_production()
    elif args.command == "build":
        evidence = json.loads(Path(args.evidence).read_text())
        print(build(args.root, args.wheelhouse, args.output, args.revision, evidence))
    elif args.extract:
        extract(args.archive, args.sha256, args.revision, args.extract, expected_runtime=runtime())
    else:
        validate(args.archive, args.sha256, args.revision)


if __name__ == "__main__":
    try:
        main()
    except (ReleaseError, OSError, ValueError, tarfile.TarError):
        raise SystemExit("release validation failed; no deployment performed") from None

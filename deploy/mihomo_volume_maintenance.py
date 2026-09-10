#!/usr/bin/env python3
"""Back up and remove exact unreferenced Mihomo anonymous data volumes."""
import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


ANONYMOUS_NAME = re.compile(r"[0-9a-f]{64}\Z")
RUNTIME_NAME = re.compile(r"/tgyunying-mihomo-\d{3}\Z")
DATA_DESTINATION = "/root/.config/mihomo"
EXPECTED_FILES = frozenset({"cache.db", "config.yaml", "geoip.dat", "geoip.metadb", "geosite.dat", "test"})
GEO_FILES = frozenset({"geoip.dat", "geoip.metadb", "geosite.dat"})
CHUNK_BYTES = 1024 * 1024
LOCK_PATH = "/run/lock/tgyunying-antigravity-provider.lock"
VOLUME_ROOT = Path("/var/lib/docker/volumes")


def command(arguments):
    return subprocess.check_output(arguments, universal_newlines=True).strip()


def digest_file(path):
    with path.open("rb") as stream:
        return digest_stream(stream)


def digest_stream(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(CHUNK_BYTES), b""):
        digest.update(chunk)
    return digest.hexdigest()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        json.dump(value, output, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def runtime_manifest():
    ids = command(["docker", "ps", "-aq"]).split()
    containers = json.loads(command(["docker", "inspect"] + ids)) if ids else []
    return sorted([{"id": item["Id"], "name": item["Name"], "image": item["Image"],
                    "running": item["State"]["Running"],
                    "binds": sorted(mount["Source"] for mount in item["Mounts"] if mount["Type"] == "bind"),
                    "volumes": sorted(mount["Name"] for mount in item["Mounts"] if mount["Type"] == "volume")}
                   for item in containers], key=lambda item: item["id"])


def referenced_names(runtime):
    return {name for item in runtime for name in item["volumes"]}


def volume_is_referenced(name, runtime):
    if name in referenced_names(runtime):
        return True
    root = str(VOLUME_ROOT / name / "_data")
    sources = (str(Path(source).resolve()) for item in runtime for source in item.get("binds", []))
    return any(os.path.commonpath([source, root]) in (source, root) for source in sources)


def file_record(path):
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("mihomo_volume_non_regular_file")
    return {"name": path.name, "size": metadata.st_size, "sha256": digest_file(path),
            "mode": stat.S_IMODE(metadata.st_mode), "uid": metadata.st_uid,
            "gid": metadata.st_gid, "mtime_ns": metadata.st_mtime_ns}


def volume_record(volume):
    name = volume["Name"]
    root = Path(volume["Mountpoint"])
    if (not ANONYMOUS_NAME.fullmatch(name) or root.resolve() != root or
            root != VOLUME_ROOT / name / "_data" or
            volume["Driver"] != "local" or volume.get("Options") or
            volume.get("Labels") not in (None, {}, {"com.docker.volume.anonymous": ""})):
        raise ValueError("mihomo_volume_identity_invalid")
    if {path.name for path in root.iterdir()} != EXPECTED_FILES:
        raise ValueError("mihomo_volume_file_set_mismatch")
    records = [file_record(root / name) for name in sorted(EXPECTED_FILES)]
    if next(item["size"] for item in records if item["name"] == "config.yaml") != 0:
        raise ValueError("mihomo_volume_has_configuration")
    metadata = root.stat()
    return {"volume": volume, "files": records,
            "root_mode": stat.S_IMODE(metadata.st_mode), "root_uid": metadata.st_uid, "root_gid": metadata.st_gid}


def geo_identity(record):
    return {item["name"]: item["sha256"] for item in record["files"] if item["name"] in GEO_FILES}


def build_preview():
    runtime = runtime_manifest()
    volumes = json.loads(command(["docker", "volume", "inspect"] + command(["docker", "volume", "ls", "-q"]).split()))
    by_name = {volume["Name"]: volume for volume in volumes}
    proxy_names = {name for item in runtime if RUNTIME_NAME.fullmatch(item["name"]) for name in item["volumes"]}
    baseline = [geo_identity(volume_record(by_name[name])) for name in sorted(proxy_names) if ANONYMOUS_NAME.fullmatch(name)]
    if not baseline:
        raise RuntimeError("mihomo_volume_live_baseline_missing")
    targets = []
    rejected = []
    for volume in sorted(volumes, key=lambda item: item["Name"]):
        if volume_is_referenced(volume["Name"], runtime) or not ANONYMOUS_NAME.fullmatch(volume["Name"]):
            continue
        try:
            record = volume_record(volume)
            if geo_identity(record) not in baseline:
                raise ValueError("mihomo_volume_geo_source_mismatch")
        except ValueError as exc:
            rejected.append({"name": volume["Name"], "reason": str(exc)})
            continue
        targets.append(record)
    manifest = {"version": 1, "release": str(Path("/data/tgyunying/current").resolve()),
                "runtime": runtime, "targets": targets, "rejected": rejected}
    return {**manifest, "fingerprint": fingerprint(manifest)}


def backup_record(record, directory, verified):
    root = Path(record["volume"]["Mountpoint"])
    completed = set(verified)
    for item in record["files"]:
        digest = item["sha256"]
        if digest in completed:
            continue
        destination = directory / (digest + ".gz")
        with destination.open("xb") as output:
            with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as compressed:
                with (root / item["name"]).open("rb") as source:
                    shutil.copyfileobj(source, compressed, CHUNK_BYTES)
            output.flush()
            os.fsync(output.fileno())
        with gzip.open(str(destination), "rb") as source:
            if digest_stream(source) != digest:
                raise RuntimeError("mihomo_volume_backup_hash_mismatch")
        completed.add(digest)
    return frozenset(completed)


def verify_current(manifest):
    if str(Path("/data/tgyunying/current").resolve()) != manifest["release"]:
        raise RuntimeError("mihomo_volume_release_drift")
    if runtime_manifest() != manifest["runtime"]:
        raise RuntimeError("mihomo_volume_runtime_drift")


def apply_manifest(manifest, *, output, expected_fingerprint):
    payload = {key: value for key, value in manifest.items() if key != "fingerprint"}
    if fingerprint(payload) != expected_fingerprint or manifest["fingerprint"] != expected_fingerprint:
        raise ValueError("mihomo_volume_manifest_hash_mismatch")
    verify_current(manifest)
    if any(volume_is_referenced(item["volume"]["Name"], manifest["runtime"]) for item in manifest["targets"]):
        raise ValueError("mihomo_volume_target_is_referenced")
    backups = output / "blobs"
    backups.mkdir()
    verified = set()
    for record in manifest["targets"]:
        current = json.loads(command(["docker", "volume", "inspect", record["volume"]["Name"]]))[0]
        if volume_record(current) != record:
            raise RuntimeError("mihomo_volume_content_drift")
        verified = backup_record(record, backups, verified)
    progress = {"status": "applying", "removed": [], "fingerprint": expected_fingerprint}
    save(output / "result.json", progress)
    for record in manifest["targets"]:
        verify_current(manifest)
        name = record["volume"]["Name"]
        current = json.loads(command(["docker", "volume", "inspect", name]))[0]
        if volume_record(current) != record:
            raise RuntimeError("mihomo_volume_content_drift")
        command(["docker", "volume", "rm", name])
        progress["removed"].append(name)
        save(output / "result.json", progress)
    verify_current(manifest)
    remaining = set(command(["docker", "volume", "ls", "-q"]).split())
    if remaining.intersection(progress["removed"]):
        raise RuntimeError("mihomo_volume_readback_failed")
    save(output / "result.json", {**progress, "status": "persisted_verified",
                                 "backup_blobs": len(verified), "filesystem": command(["df", "-k", "/data"])})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preview", "apply"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--expected-fingerprint", default="")
    parser.add_argument("--actor", required=True)
    parser.add_argument("--approval-ref", required=True)
    args = parser.parse_args()
    if not args.actor.strip() or not args.approval_ref.strip():
        raise ValueError("mihomo_volume_audit_identity_required")
    os.umask(0o077)
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "operation.json", {"actor": args.actor, "approval_ref": args.approval_ref, "mode": args.mode})
    with open(LOCK_PATH, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.mode == "preview":
            manifest = build_preview()
            save(args.output / "manifest.json", manifest)
            print(json.dumps({"count": len(manifest["targets"]), "fingerprint": manifest["fingerprint"]}))
        else:
            if args.manifest is None:
                raise ValueError("mihomo_volume_manifest_required")
            manifest = json.loads(args.manifest.read_text())
            save(args.output / "manifest.json", manifest)
            try:
                apply_manifest(manifest, output=args.output, expected_fingerprint=args.expected_fingerprint)
            except Exception as exc:
                result_path = args.output / "result.json"
                progress = json.loads(result_path.read_text()) if result_path.exists() else {"removed": []}
                save(result_path, {**progress, "status": "failed", "error_type": type(exc).__name__})
                raise
            print(json.dumps({"status": "persisted_verified", "count": len(manifest["targets"])}))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


CONTAINER_NAME = re.compile(r"tgyunying-mihomo-\d{3}")
CONFIG_DESTINATION = "/root/.config/mihomo/config.yaml"
DEFAULT_AUDIT_DIR = "/data/tgyunying/shared/mihomo/retirement-audits"
DB_QUERY_CODE = """
import json
import os
from app.database import SessionLocal
from app.services.proxy_runtime_retirement import snapshot_proxy_runtimes
names = tuple(json.loads(os.environ['PROXY_RUNTIME_TARGETS_JSON']))
with SessionLocal() as session:
    records = snapshot_proxy_runtimes(session, names)
    print(json.dumps([
        {'payload': record.payload(), 'state_hash': record.state_hash()}
        for record in records
    ], ensure_ascii=True, sort_keys=True))
"""
DB_APPLY_CODE = """
import json
import os
from app.database import SessionLocal
from app.services.proxy_runtime_retirement import (
    ProxyRetirementRequest,
    retire_proxy_runtimes,
)
request = ProxyRetirementRequest(
    expected_state_hashes=json.loads(os.environ['PROXY_RUNTIME_EXPECTED_HASHES_JSON']),
    actor=os.environ['PROXY_RUNTIME_ACTOR'],
    approval_ref=os.environ['PROXY_RUNTIME_APPROVAL_REF'],
)
with SessionLocal.begin() as session:
    records = retire_proxy_runtimes(session, request)
print(json.dumps({'retired': [record.name for record in records]}, sort_keys=True))
"""
DB_AUDIT_CODE = """
import json
import os
from sqlalchemy import func, select
from app.database import SessionLocal
from app.models import AuditLog
targets = json.loads(os.environ['PROXY_RUNTIME_TARGET_IDS_JSON'])
actor = os.environ['PROXY_RUNTIME_ACTOR']
approval_ref = os.environ['PROXY_RUNTIME_APPROVAL_REF']
result = {}
with SessionLocal() as session:
    for name, target_id in targets.items():
        count = session.scalar(select(func.count(AuditLog.id)).where(
            AuditLog.target_type == 'account_proxy',
            AuditLog.target_id == str(target_id),
            AuditLog.actor == actor,
            AuditLog.action == '退役零消费者代理运行时',
            AuditLog.detail.contains(approval_ref),
        )) or 0
        result[name] = int(count)
print(json.dumps(result, ensure_ascii=True, sort_keys=True))
"""


def main(argv=None):
    args = parse_args(argv)
    try:
        preview = build_preview(args)
        if not args.apply:
            print_json({"mode": "preview", **preview})
            return 0
        result = apply_retirement(args, preview)
        print_json({"mode": "apply", **result})
        return 0
    except RuntimeError as exc:
        print_json(
            {
                "mode": "apply" if args.apply else "preview",
                "status": "failed",
                "error": str(exc),
            }
        )
        return 1


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Disable and stop exact zero-consumer Mihomo runtimes."
    )
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--backend-container", default="tgyunying-backend")
    parser.add_argument("--current-release-link", default="/data/tgyunying/current")
    parser.add_argument("--audit-dir", default=DEFAULT_AUDIT_DIR)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-deployed-sha", default="")
    parser.add_argument("--expected-manifest-hash", default="")
    parser.add_argument("--actor", default="")
    parser.add_argument("--approval-ref", default="")
    return parser.parse_args(argv)


def build_preview(args):
    targets = validate_targets(args.target)
    deployed_release = str(Path(args.current_release_link).resolve())
    db_records = query_db_records(args.backend_container, targets)
    runtime_records = tuple(inspect_runtime(name) for name in targets)
    non_target_hash = non_target_runtime_manifest_hash(set(targets))
    records = combine_records(db_records, runtime_records)
    manifest = {
        "version": 1,
        "deployed_release": deployed_release,
        "targets": records,
        "non_target_runtime_manifest_hash": non_target_hash,
    }
    return {
        "status": "preview",
        **manifest,
        "manifest_hash": manifest_hash(manifest),
    }


def apply_retirement(
    args,
    preview,
):
    validate_apply_inputs(args, preview)
    current = build_preview(args)
    validate_apply_inputs(args, current)
    validate_zero_consumers(current)
    audit_path = write_audit(args, current, status="started")
    stopped = []
    try:
        apply_db_retirement(args, current)
        for record in current["targets"]:
            stop_runtime(record)
            stopped.append(record["name"])
        readback = verify_readback(args, current)
    except RuntimeError as exc:
        write_audit(
            args,
            current,
            status="failed",
            path=audit_path,
            stopped=stopped,
            error=str(exc),
        )
        raise
    write_audit(
        args,
        current,
        status="completed",
        path=audit_path,
        stopped=stopped,
        readback=readback,
    )
    return {
        "status": "completed",
        "audit_path": str(audit_path),
        "stopped": stopped,
        "readback": readback,
    }


def validate_targets(raw_targets):
    targets = tuple(sorted(set(raw_targets)))
    if not targets:
        raise RuntimeError("proxy_runtime_targets_required")
    invalid = [name for name in targets if not CONTAINER_NAME.fullmatch(name)]
    if invalid:
        raise RuntimeError(f"proxy_runtime_target_invalid:{','.join(invalid)}")
    return targets


def validate_apply_inputs(
    args,
    preview,
):
    required = {
        "expected_deployed_sha": args.expected_deployed_sha,
        "expected_manifest_hash": args.expected_manifest_hash,
        "actor": args.actor,
        "approval_ref": args.approval_ref,
    }
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise RuntimeError(f"proxy_runtime_apply_input_required:{','.join(missing)}")
    release_name = Path(str(preview["deployed_release"])).name
    if args.expected_deployed_sha[:8] not in release_name:
        raise RuntimeError("proxy_runtime_deployed_sha_mismatch")
    if preview["manifest_hash"] != args.expected_manifest_hash:
        raise RuntimeError("proxy_runtime_manifest_hash_mismatch")


def validate_zero_consumers(preview):
    for record in preview["targets"]:
        consumers = record["db"]["payload"]["consumers"]
        if any(int(value) for value in consumers.values()):
            raise RuntimeError(f"proxy_runtime_has_consumers:{record['name']}")
        runtime = record["runtime"]
        if not runtime["running"]:
            raise RuntimeError(f"proxy_runtime_not_running:{record['name']}")
        if runtime["restart_policy"] == "no":
            raise RuntimeError(f"proxy_runtime_restart_policy_already_no:{record['name']}")


def query_db_records(
    backend_container,
    targets,
):
    result = run_command(
        [
            "docker",
            "exec",
            "-e",
            f"PROXY_RUNTIME_TARGETS_JSON={json.dumps(targets)}",
            backend_container,
            "python",
            "-c",
            DB_QUERY_CODE,
        ],
        capture_output=True,
    )
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("proxy_runtime_db_preview_invalid_json") from exc
    if not isinstance(records, list) or len(records) != len(targets):
        raise RuntimeError("proxy_runtime_db_preview_incomplete")
    return tuple(records)


def apply_db_retirement(
    args,
    preview,
):
    expected = {
        record["name"]: record["db"]["state_hash"]
        for record in preview["targets"]
    }
    run_command(
        [
            "docker",
            "exec",
            "-e",
            f"PROXY_RUNTIME_EXPECTED_HASHES_JSON={json.dumps(expected)}",
            "-e",
            f"PROXY_RUNTIME_ACTOR={args.actor}",
            "-e",
            f"PROXY_RUNTIME_APPROVAL_REF={args.approval_ref}",
            args.backend_container,
            "python",
            "-c",
            DB_APPLY_CODE,
        ]
    )


def inspect_runtime(name):
    result = run_command(["docker", "inspect", name], capture_output=True)
    try:
        payload = json.loads(result.stdout)[0]
    except (IndexError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"proxy_runtime_inspect_invalid:{name}") from exc
    config = config_mount(payload, name)
    return {
        "name": name,
        "container_id": payload["Id"],
        "image_id": payload["Image"],
        "running": bool(payload["State"]["Running"]),
        "restart_policy": payload["HostConfig"]["RestartPolicy"]["Name"],
        "config_source": config["Source"],
        "config_sha256": file_sha256(Path(config["Source"])),
    }


def config_mount(payload, name):
    mounts = [
        mount
        for mount in payload.get("Mounts", [])
        if mount.get("Destination") == CONFIG_DESTINATION
    ]
    if len(mounts) != 1 or mounts[0].get("RW") is not False:
        raise RuntimeError(f"proxy_runtime_config_mount_invalid:{name}")
    source = Path(str(mounts[0].get("Source") or ""))
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"proxy_runtime_config_source_invalid:{name}")
    return mounts[0]


def combine_records(
    db_records,
    runtime_records,
):
    db_by_name = {record["payload"]["name"]: record for record in db_records}
    combined = []
    for runtime in runtime_records:
        name = runtime["name"]
        if name not in db_by_name:
            raise RuntimeError(f"proxy_runtime_db_target_missing:{name}")
        combined.append({"name": name, "db": db_by_name[name], "runtime": runtime})
    return combined


def non_target_runtime_manifest_hash(targets):
    result = run_command(
        ["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True
    )
    names = sorted(
        name
        for name in result.stdout.splitlines()
        if CONTAINER_NAME.fullmatch(name) and name not in targets
    )
    records = [inspect_runtime(name) for name in names]
    return manifest_hash({"version": 1, "runtimes": records})


def stop_runtime(record):
    name = record["name"]
    current = inspect_runtime(name)
    if current != record["runtime"]:
        raise RuntimeError(f"proxy_runtime_container_drift:{name}")
    run_command(["docker", "update", "--restart=no", name])
    run_command(["docker", "stop", "--time", "30", name])


def verify_readback(
    args,
    before,
):
    targets = tuple(record["name"] for record in before["targets"])
    db_records = query_db_records(args.backend_container, targets)
    by_name = {record["payload"]["name"]: record for record in db_records}
    for name in targets:
        verify_db_readback(by_name[name], name, args.approval_ref)
        verify_runtime_readback(inspect_runtime(name), name)
    audit_counts = query_audit_counts(args, before)
    if any(audit_counts.get(name) != 1 for name in targets):
        raise RuntimeError("proxy_runtime_audit_readback_failed")
    after_non_target_hash = non_target_runtime_manifest_hash(set(targets))
    if after_non_target_hash != before["non_target_runtime_manifest_hash"]:
        raise RuntimeError("proxy_runtime_non_target_manifest_changed")
    return {
        "disabled_count": len(targets),
        "stopped_count": len(targets),
        "audit_count": sum(audit_counts.values()),
        "non_target_runtime_manifest_unchanged": True,
    }


def query_audit_counts(
    args,
    preview,
):
    targets = {
        record["name"]: record["db"]["payload"]["id"]
        for record in preview["targets"]
    }
    result = run_command(
        [
            "docker",
            "exec",
            "-e",
            f"PROXY_RUNTIME_TARGET_IDS_JSON={json.dumps(targets)}",
            "-e",
            f"PROXY_RUNTIME_ACTOR={args.actor}",
            "-e",
            f"PROXY_RUNTIME_APPROVAL_REF={args.approval_ref}",
            args.backend_container,
            "python",
            "-c",
            DB_AUDIT_CODE,
        ],
        capture_output=True,
    )
    try:
        counts = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("proxy_runtime_audit_readback_invalid_json") from exc
    return {str(name): int(count) for name, count in counts.items()}


def verify_db_readback(record, name, approval_ref):
    payload = record["payload"]
    if payload["status"] != "disabled" or payload["alert_status"] != "disabled":
        raise RuntimeError(f"proxy_runtime_db_readback_failed:{name}")
    if approval_ref not in payload["disabled_reason"]:
        raise RuntimeError(f"proxy_runtime_approval_readback_failed:{name}")
    if any(int(value) for value in payload["consumers"].values()):
        raise RuntimeError(f"proxy_runtime_consumer_readback_failed:{name}")


def verify_runtime_readback(record, name):
    if record["running"] or record["restart_policy"] != "no":
        raise RuntimeError(f"proxy_runtime_stop_readback_failed:{name}")


def write_audit(
    args,
    preview,
    *,
    status,
    path=None,
    stopped=None,
    error="",
    readback=None,
):
    audit_dir = Path(args.audit_dir)
    audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = path or audit_dir / retirement_audit_name()
    payload = {
        "actor": args.actor,
        "approval_ref": args.approval_ref,
        "error": error,
        "preview": preview,
        "readback": readback or {},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "stopped": stopped or [],
    }
    write_json_atomic(path, payload)
    return path


def retirement_audit_name():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{timestamp}_{uuid.uuid4().hex}.json"


def write_json_atomic(path, payload):
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, encoding="utf-8", delete=False
    ) as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        handle.write("\n")
        temporary = handle.name
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def manifest_hash(payload):
    encoded = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    command,
    *,
    capture_output=False,
):
    options = {"check": False, "universal_newlines": True}
    if capture_output:
        options.update({"stdout": subprocess.PIPE, "stderr": subprocess.PIPE})
    result = subprocess.run(command, **options)
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        suffix = f":{detail[-1]}" if detail else ""
        raise RuntimeError(f"proxy_runtime_command_failed:{command[0]}{suffix}")
    return result


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())

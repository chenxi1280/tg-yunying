#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


CONFIG_NAME = re.compile(r"tgyunying-mihomo-\d{3}\.yaml$")
CONTAINER_NAME = re.compile(r"tgyunying-mihomo-\d{3}$")
PINNED_IMAGE = re.compile(r"metacubex/mihomo@sha256:[0-9a-f]{64}$")
MIHOMO_CONFIG_PATH = "/root/.config/mihomo/config.yaml"
EGRESS_URL = "https://api.ipify.org"


class ConfigSource:
    def __init__(self, name, path, sha256):
        self.name = name
        self.path = path
        self.sha256 = sha256


def main(argv=None):
    args = parse_args(argv)
    try:
        preview = build_preview(args)
        if not args.apply:
            print_json({"mode": "preview", **public_preview(preview)})
            return 0
        result = apply_restore(args, preview)
        print_json({"mode": "apply", **result})
        return 0
    except RuntimeError as exc:
        print_json({"mode": "apply" if args.apply else "preview", "status": "failed", "error": str(exc)})
        return 1


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Restore only missing Mihomo runtime containers from verified configs.")
    parser.add_argument("--primary-config-dir", required=True)
    parser.add_argument("--supplemental-config-path", action="append", default=[])
    parser.add_argument("--image", required=True)
    parser.add_argument("--expected-deployed-sha", default="")
    parser.add_argument("--current-release-link", default="/data/tgyunying/current")
    parser.add_argument("--backend-container", default="tgyunying-backend")
    parser.add_argument("--network", default="infra_default")
    parser.add_argument("--audit-dir", default="/data/tgyunying/shared/mihomo/restore-audits")
    parser.add_argument("--allowed-missing-proxy-name", action="append", default=[])
    parser.add_argument("--mark-allowed-missing-proxies-unhealthy", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-config-manifest-hash", default="")
    parser.add_argument("--expected-proxy-manifest-hash", default="")
    parser.add_argument("--expected-target-manifest-hash", default="")
    parser.add_argument("--approval-ref", default="")
    return parser.parse_args(argv)


def build_preview(args):
    require_pinned_image(args.image)
    configs = collect_config_sources(args.primary_config_dir, args.supplemental_config_path)
    proxies = query_proxy_records(args.backend_container)
    existing = list_mihomo_containers()
    proxy_names = {row["name"] for row in proxies}
    config_names = set(configs)
    target_names = sorted(config_names & proxy_names)
    config_manifest = manifest_hash({
        "version": 1,
        "configs": [config_manifest_item(configs[name]) for name in sorted(configs)],
    })
    proxy_manifest = manifest_hash({"version": 1, "proxies": proxies})
    target_manifest = manifest_hash({
        "version": 1,
        "backend_container": args.backend_container,
        "image": args.image,
        "network": args.network,
        "targets": [config_manifest_item(configs[name]) for name in target_names],
    })
    return {
        "status": "preview",
        "config_manifest_hash": config_manifest,
        "proxy_manifest_hash": proxy_manifest,
        "target_manifest_hash": target_manifest,
        "config_count": len(configs),
        "proxy_count": len(proxies),
        "target_count": len(target_names),
        "target_names": target_names,
        "missing_proxy_configs": sorted(proxy_names - config_names),
        "unregistered_configs": sorted(config_names - proxy_names),
        "existing_containers": existing,
        "proxy_records": proxies,
    }


def apply_restore(args, preview):
    validate_apply_inputs(args, preview)
    preview = build_preview(args)
    validate_apply_inputs(args, preview)
    audit_path = write_audit(args, preview, status="started", created_names=[])
    created_names = []
    retired_names = []
    try:
        ensure_deployed_sha(args.expected_deployed_sha, Path(args.current_release_link))
        ensure_backend_healthy(args.backend_container)
        run_command(["docker", "pull", args.image])
        configs = collect_config_sources(args.primary_config_dir, args.supplemental_config_path)
        for name in preview["target_names"]:
            start_container(name, configs[name], args.network, args.image)
            created_names.append(name)
        for name in created_names:
            ensure_proxy_egress(args.backend_container, name)
        if args.mark_allowed_missing_proxies_unhealthy:
            retired_names = mark_unconfigured_proxies_unhealthy(
                args.backend_container,
                preview["proxy_records"],
                preview["missing_proxy_configs"],
                args.approval_ref,
            )
    except RuntimeError as exc:
        remove_created_containers(created_names)
        write_audit(
            args,
            preview,
            status="failed",
            created_names=created_names,
            error=str(exc),
            path=audit_path,
            retired_names=retired_names,
        )
        raise
    write_audit(
        args,
        preview,
        status="completed",
        created_names=created_names,
        path=audit_path,
        retired_names=retired_names,
    )
    return {
        "status": "completed",
        "audit_path": str(audit_path),
        "created_count": len(created_names),
        "created_names": created_names,
        "excluded_missing_proxy_configs": preview["missing_proxy_configs"],
        "retired_unconfigured_proxies": retired_names,
        "unregistered_configs": preview["unregistered_configs"],
        "target_manifest_hash": preview["target_manifest_hash"],
    }


def validate_apply_inputs(args, preview):
    required = {
        "expected_deployed_sha": args.expected_deployed_sha,
        "expected_config_manifest_hash": args.expected_config_manifest_hash,
        "expected_proxy_manifest_hash": args.expected_proxy_manifest_hash,
        "expected_target_manifest_hash": args.expected_target_manifest_hash,
        "approval_ref": args.approval_ref,
    }
    missing = sorted(name for name, value in required.items() if not value.strip())
    if missing:
        raise RuntimeError(f"apply_input_required:{','.join(missing)}")
    expected = {
        "config_manifest_hash": args.expected_config_manifest_hash,
        "proxy_manifest_hash": args.expected_proxy_manifest_hash,
        "target_manifest_hash": args.expected_target_manifest_hash,
    }
    for field, expected_hash in expected.items():
        if preview[field] != expected_hash:
            raise RuntimeError(f"manifest_hash_mismatch:{field}")
    if preview["existing_containers"]:
        raise RuntimeError("mihomo_existing_containers_present")
    allowed_missing = sorted(set(args.allowed_missing_proxy_name))
    if preview["missing_proxy_configs"] != allowed_missing:
        raise RuntimeError("missing_proxy_config_scope_mismatch")
    records = {row["name"]: row for row in preview["proxy_records"]}
    for name in allowed_missing:
        record = records[name]
        if record["active_slot_bindings"] or record["direct_accounts"]:
            raise RuntimeError(f"missing_proxy_has_active_consumers:{name}")
    if not preview["target_names"]:
        raise RuntimeError("mihomo_restore_target_empty")


def collect_config_sources(primary_directory, supplemental_paths):
    sources = {}
    directory = Path(primary_directory)
    if not directory.is_dir():
        raise RuntimeError(f"config_dir_not_found:{directory}")
    for path in sorted(directory.glob("tgyunying-mihomo-*.yaml")):
        add_config_source(sources, path)
    for raw_path in supplemental_paths:
        add_config_source(sources, Path(raw_path))
    if not sources:
        raise RuntimeError("mihomo_config_source_empty")
    return sources


def add_config_source(sources, path):
    if not CONFIG_NAME.fullmatch(path.name):
        raise RuntimeError(f"invalid_config_name:{path.name}")
    if path.is_symlink():
        raise RuntimeError(f"config_source_symlink:{path.name}")
    if not path.is_file():
        raise RuntimeError(f"config_source_not_file:{path.name}")
    source = ConfigSource(path.stem, path, file_sha256(path))
    existing = sources.get(source.name)
    if existing and existing.sha256 != source.sha256:
        raise RuntimeError(f"duplicate_config_hash_mismatch:{source.name}")
    sources.setdefault(source.name, source)


def query_proxy_records(backend_container):
    code = """
import json
from sqlalchemy import func, select
from app.database import SessionLocal
from app.models import AccountProxy, AccountProxyBinding, TgAccount
with SessionLocal() as session:
    rows = list(session.scalars(select(AccountProxy).where(AccountProxy.name.like('tgyunying-mihomo-%')).order_by(AccountProxy.name)))
    payload = []
    for proxy in rows:
        direct = session.scalar(select(func.count(TgAccount.id)).where(TgAccount.proxy_id == proxy.id)) or 0
        bound = session.scalar(select(func.count(AccountProxyBinding.id)).where(AccountProxyBinding.proxy_id == proxy.id, AccountProxyBinding.status == 'active', AccountProxyBinding.unbound_at.is_(None))) or 0
        payload.append({'id': proxy.id, 'name': proxy.name, 'host': proxy.host, 'port': proxy.port, 'status': proxy.status, 'direct_accounts': int(direct), 'active_slot_bindings': int(bound)})
print(json.dumps(payload, ensure_ascii=True, sort_keys=True))
"""
    result = run_command(["docker", "exec", backend_container, "python", "-c", code], capture_output=True)
    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("proxy_record_query_invalid_json") from exc
    if not isinstance(records, list):
        raise RuntimeError("proxy_record_query_invalid_payload")
    names = [record.get("name", "") for record in records]
    if any(not CONTAINER_NAME.fullmatch(name) for name in names) or len(set(names)) != len(names):
        raise RuntimeError("proxy_record_name_invalid")
    return sorted(records, key=lambda row: row["name"])


def list_mihomo_containers():
    result = run_command(["docker", "ps", "-a", "--format", "{{.Names}}"], capture_output=True)
    return sorted(name for name in result.stdout.splitlines() if CONTAINER_NAME.fullmatch(name))


def ensure_deployed_sha(expected_sha, current_release_link):
    resolved = current_release_link.resolve()
    if expected_sha[:8] not in resolved.name:
        raise RuntimeError("deployed_sha_mismatch")


def ensure_backend_healthy(backend_container):
    result = run_command(
        ["docker", "inspect", backend_container, "--format", "{{.State.Health.Status}}"],
        capture_output=True,
    )
    if result.stdout.strip() != "healthy":
        raise RuntimeError("backend_not_healthy")


def start_container(name, config, network, image):
    run_command([
        "docker",
        "run",
        "-d",
        "--restart",
        "unless-stopped",
        "--name",
        name,
        "--network",
        network,
        "-v",
        f"{config.path}:{MIHOMO_CONFIG_PATH}:ro",
        image,
    ])


def ensure_proxy_egress(backend_container, name):
    result = run_command([
        "docker",
        "exec",
        backend_container,
        "curl",
        "-fsS",
        "--max-time",
        "12",
        "-x",
        f"socks5h://{name}:7890",
        EGRESS_URL,
    ], check=False, capture_output=True)
    if result.returncode or not result.stdout.strip():
        raise RuntimeError(f"proxy_egress_failed:{name}")


def mark_unconfigured_proxies_unhealthy(backend_container, records, names, approval_ref):
    by_name = {record["name"]: record for record in records}
    targets = [{"id": by_name[name]["id"], "name": name} for name in names]
    code = f"""
import json
from sqlalchemy import select
from app.database import SessionLocal
from app.models import AccountProxy
from app.services.risk_control import check_account_proxy
targets = {json.dumps(targets, ensure_ascii=True)}
approval_ref = {json.dumps(approval_ref, ensure_ascii=True)}
result = []
with SessionLocal() as session:
    for item in targets:
        proxy = session.scalar(select(AccountProxy).where(AccountProxy.id == item['id']))
        if proxy is None or proxy.name != item['name']:
            raise RuntimeError('proxy_record_drift')
        result.append(check_account_proxy(session, 1, proxy.id, check_type='quick', reason=f'unconfigured_mihomo_runtime; approval_ref={{approval_ref}}', actor='production-mihomo-restore'))
print(json.dumps(result, ensure_ascii=True, sort_keys=True))
"""
    result = run_command(["docker", "exec", backend_container, "python", "-c", code], capture_output=True)
    try:
        checks = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("unconfigured_proxy_status_update_invalid_json") from exc
    if any(check.get("status") != "unhealthy" for check in checks):
        raise RuntimeError("unconfigured_proxy_did_not_become_unhealthy")
    return names


def remove_created_containers(names):
    for name in reversed(names):
        run_command(["docker", "rm", "-f", name], check=False)


def write_audit(
    args,
    preview,
    *,
    status: str,
    created_names,
    error: str = "",
    path=None,
    retired_names=None,
):
    audit_dir = Path(args.audit_dir)
    audit_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = path or audit_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}_{uuid.uuid4().hex}.json"
    payload = {
        "approval_ref": args.approval_ref,
        "created_names": created_names,
        "error": error,
        "expected_deployed_sha": args.expected_deployed_sha,
        "image": args.image,
        "preview": preview,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "retired_names": retired_names or [],
        "status": status,
    }
    with tempfile.NamedTemporaryFile("w", dir=audit_dir, encoding="utf-8", delete=False) as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True))
        handle.write("\n")
        temp_name = handle.name
    os.chmod(temp_name, 0o600)
    os.replace(temp_name, path)
    return path


def require_pinned_image(image):
    if not PINNED_IMAGE.fullmatch(image):
        raise RuntimeError("mihomo_image_must_be_pinned_digest")


def config_manifest_item(source):
    return {"name": source.name, "sha256": source.sha256}


def public_preview(preview):
    return {key: value for key, value in preview.items() if key != "proxy_records"}


def manifest_hash(payload):
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_command(
    command,
    *,
    check: bool = True,
    capture_output: bool = False,
):
    options = {"check": False, "universal_newlines": True}
    if capture_output:
        options["stdout"] = subprocess.PIPE
        options["stderr"] = subprocess.PIPE
    result = subprocess.run(command, **options)
    if check and result.returncode:
        raise RuntimeError(f"command_failed:{command[0]}")
    return result


def print_json(payload):
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())

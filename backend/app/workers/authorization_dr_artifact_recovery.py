from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from app.integrations.telegram import AuthorizationIdentity, DeveloperAppCredentials, create_gateway
from app.workers.authorization_dr_node import (
    DrNodeClient,
    NodeConfig,
    RestoreProbeInput,
    _decrypt_session,
    _next_inventory_sequence,
    _restore_probe,
    _verify_expected_egress,
    _wrapped_dek_ciphertext,
    _write_inventory,
)
from app.workers.authorization_dr_stage_client import post_bundle_stages


def recover_artifact(config: NodeConfig, operation_id: str) -> dict:
    _verify_expected_egress(config)
    client = DrNodeClient(config)
    gateway = create_gateway()
    client.heartbeat(0)
    claim = client.post(
        f"/internal/v1/authorization-dr/operations/{operation_id}/reconcile-claim",
        {},
    )
    owner = _owner_payload(claim)
    client.heartbeat(1)
    try:
        material = client.post(
            f"/internal/v1/authorization-dr/operations/{operation_id}/reconcile-probe-material",
            owner,
        )
        receipt, object_key, identity = _recover_receipt(config, claim, material, gateway)
        post_bundle_stages(client, operation_id, owner, receipt)
        client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/wake-bundle", {**owner, **receipt})
        probe_input = RestoreProbeInput(config, claim, material, object_key, identity)
        probe = _restore_probe(gateway, probe_input)
        client.post(
            f"/internal/v1/authorization-dr/operations/{operation_id}/wake-bundle/restore-probe",
            {**owner, **probe},
        )
        result = client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/slot-commit", owner)
        return {"operation_id": operation_id, "status": "succeeded", "decision_id": result["decision_id"]}
    finally:
        client.heartbeat(0)


def _recover_receipt(config, claim: dict, material: dict, gateway) -> tuple[dict, str, AuthorizationIdentity]:
    generation = int(claim["target_generation"])
    account_id = int(claim["account_id"])
    relative = Path(str(account_id)) / f"g{generation}.bundle"
    local_path = config.local_dir / relative
    object_key = f"{config.object_prefix.strip('/')}/{relative.as_posix()}".lstrip("/")
    envelope = local_path.read_bytes()
    digest = hashlib.sha256(envelope).hexdigest()
    if digest != claim["expected_ciphertext_digest"]:
        raise RuntimeError("local artifact digest does not match approved reconciliation evidence")
    dek = config.dek_protector.unwrap(_wrapped_dek_ciphertext(envelope))
    raw_session = _decrypt_session(envelope, dek)
    identity = gateway.authorization_identity(raw_session, _credentials(claim, material))
    copies = [_verified_copy(local_path, envelope, "local_persistent", local_path.name)]
    copies.append(_ensure_snapshot_copy(config, object_key, envelope))
    sequence, inventory_digest = _ensure_inventory(config, claim, object_key, digest)
    payload = json.loads(envelope)
    receipt = _receipt(config, claim, identity, digest, payload, sequence, inventory_digest, copies)
    return receipt, object_key, identity


def _ensure_snapshot_copy(config, object_key: str, envelope: bytes) -> dict:
    digest = hashlib.sha256(envelope).hexdigest()
    if config.object_store.exists(object_key):
        if config.object_store.read(object_key) != envelope:
            raise RuntimeError("snapshot artifact conflicts with approved local bytes")
        if config.snapshot_copy_kind != "remote_ssh_snapshot":
            raise RuntimeError("pre-existing object snapshot version cannot be reconstructed safely")
        version = f"sha256-{digest}"
    else:
        version = config.object_store.put_immutable(object_key, envelope)
        if config.object_store.read(object_key) != envelope:
            raise RuntimeError("snapshot artifact readback failed")
    return _verified_copy(Path(object_key), envelope, config.snapshot_copy_kind, version)


def _ensure_inventory(config, claim: dict, object_key: str, digest: str) -> tuple[int, str]:
    sequence = int(claim["expected_inventory_sequence"])
    if sequence < 1:
        sequence = _next_inventory_sequence(config)
        return sequence, _write_inventory(config, claim, object_key, digest, sequence)
    payload = _inventory_payload(claim, object_key, digest, sequence)
    key = f"{config.object_prefix.strip('/')}/inventory/{sequence}.json".lstrip("/")
    if not config.object_store.exists(key) or config.object_store.read(key) != payload:
        raise RuntimeError("approved inventory artifact is missing or changed")
    return sequence, hashlib.sha256(payload).hexdigest()


def _inventory_payload(claim: dict, object_key: str, digest: str, sequence: int) -> bytes:
    return json.dumps({
        "operation_id": claim["operation_id"],
        "account_id": claim["account_id"],
        "generation": claim["target_generation"],
        "bundle_object_ref_digest": hashlib.sha256(object_key.encode()).hexdigest(),
        "ciphertext_digest": digest,
        "inventory_sequence": sequence,
    }, sort_keys=True, separators=(",", ":")).encode()


def _verified_copy(path: Path, envelope: bytes, kind: str, version: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(envelope).hexdigest()
    ref = str(path) if kind == "local_persistent" else path.as_posix()
    suffix = "oss-" if kind != "local_persistent" else ""
    return {
        "copy_kind": kind,
        "object_ref_digest": hashlib.sha256(ref.encode()).hexdigest(),
        "ciphertext_digest": digest,
        "immutable_version": version,
        "write_receipt_digest": hashlib.sha256(f"{digest}:{suffix}write".encode()).hexdigest(),
        "readback_receipt_digest": hashlib.sha256(f"{digest}:{suffix}readback".encode()).hexdigest(),
        "write_verified_at": now,
        "readback_verified_at": now,
        "decrypt_verified_at": now,
    }


def _receipt(config, claim, identity, digest, payload, sequence, inventory_digest, copies) -> dict:
    return {
        "bundle_generation": claim["target_generation"],
        "ciphertext_digest": digest,
        "wrapped_dek_ciphertext": payload["wrapped_dek_ciphertext"],
        "kms_key_ref_digest": hashlib.sha256(config.dek_protector.key_ref.encode()).hexdigest(),
        "kms_key_version": payload["kms_key_version"],
        "auth_key_fingerprint_digest": identity.auth_key_fingerprint_digest,
        "telegram_user_id_digest": identity.telegram_user_id_digest,
        "authorization_fingerprint_digest": identity.authorization_fingerprint_digest,
        "remote_authorization_hash": identity.authorization_hash,
        "inventory_sequence": sequence,
        "inventory_manifest_digest": inventory_digest,
        "copies": copies,
    }


def _credentials(claim: dict, material: dict) -> DeveloperAppCredentials:
    return DeveloperAppCredentials(
        app_id=claim["developer_app_id"],
        api_id=material["api_id"],
        api_hash=material["api_hash"],
        credentials_version=material["credentials_version"],
        app_name=material["app_name"],
    )


def _owner_payload(claim: dict) -> dict:
    return {"owner_epoch": claim["owner_epoch"], "lease_token": claim["lease_token"]}


__all__ = ["recover_artifact"]

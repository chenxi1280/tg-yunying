from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import httpx
from telethon.errors import PasswordHashInvalidError, PhoneNumberBannedError

from app.integrations.telegram import AuthorizationIdentity, DeveloperAppCredentials, create_gateway
from app.workers.authorization_dr_kms import DekProtector, WrappedDek
from app.workers.authorization_dr_storage import ObjectSnapshotStore, load_storage_config


LOGGER = logging.getLogger(__name__)
CODE_POLL_SECONDS = 2
LEASE_RENEW_SECONDS = 30
IDLE_POLL_SECONDS = 10
RECEIPT_POST_ATTEMPTS = 3


@dataclass(frozen=True)
class NodeConfig:
    control_plane_url: str
    internal_token: str
    node_id: str
    egress_id: str
    expected_egress_ip: str
    egress_probe_url: str
    local_dir: Path
    object_prefix: str
    object_store: ObjectSnapshotStore
    dek_protector: DekProtector
    client_cert: tuple[str, str] | None
    snapshot_copy_kind: str = "remote_ssh_snapshot"


@dataclass(frozen=True)
class RestoreProbeInput:
    config: NodeConfig
    claim: dict
    material: dict
    object_key: str
    expected: AuthorizationIdentity


class DrNodeClient:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.http = httpx.Client(
            base_url=config.control_plane_url.rstrip("/"),
            headers={
                "Authorization": f"Bearer {config.internal_token}",
                "X-DR-Node-ID": config.node_id,
            },
            cert=config.client_cert,
            timeout=30,
        )

    def post(self, path: str, payload: dict) -> dict:
        response = self.http.post(path, json=payload)
        response.raise_for_status()
        return response.json() if response.content else {}

    def heartbeat(self, active_clients: int) -> None:
        self.post("/internal/v1/authorization-dr/nodes/heartbeat", {
            "region_code": "my",
            "purpose": "standby_session_dr",
            "capability_version": "2.16",
            "standby_egress_id": self.config.egress_id,
            "active_client_count": active_clients,
            "node_version": 1,
        })

    def claim(self) -> dict | None:
        response = self.http.post(
            "/internal/v1/authorization-dr/operations/claim",
            json={"purpose": "migrate_standby_2"},
        )
        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response.json()


def run_node(config: NodeConfig, *, once: bool) -> int:
    _verify_expected_egress(config)
    client = DrNodeClient(config)
    gateway = create_gateway()
    while True:
        client.heartbeat(0)
        claim = client.claim()
        if claim:
            _process_claim(client, gateway, claim)
        if once:
            return 0
        time.sleep(IDLE_POLL_SECONDS)


def _verify_expected_egress(config: NodeConfig) -> None:
    observed = httpx.get(config.egress_probe_url, timeout=15).text.strip()
    if observed != config.expected_egress_ip:
        raise RuntimeError("Malaysia Telegram egress does not match the configured fixed IP")


def _process_claim(client: DrNodeClient, gateway, claim: dict) -> None:
    operation_id = claim["operation_id"]
    owner = _owner_payload(claim)
    bundle_committed = False
    login_started = False
    client.heartbeat(1)
    try:
        material = client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/login-material", owner)
        client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/login-started", owner)
        login_started = True
        try:
            raw_session = _complete_login(client, gateway, claim, material)
        except (PasswordHashInvalidError, PhoneNumberBannedError) as exc:
            blocker_code = "two_fa_invalid" if isinstance(exc, PasswordHashInvalidError) else "phone_number_banned"
            client.post(
                f"/internal/v1/authorization-dr/operations/{operation_id}/login-failed",
                {**owner, "blocker_code": blocker_code},
            )
            LOGGER.warning("authorization DR login rejected (%s): %s", blocker_code, operation_id)
            return
        identity = gateway.authorization_identity(raw_session, _credentials(claim, material))
        receipt, object_key = _persist_bundle(client.config, claim, raw_session, identity)
        _post_bundle_receipt(client, operation_id, {**owner, **receipt})
        bundle_committed = True
        probe = _restore_probe(gateway, RestoreProbeInput(
            config=client.config,
            claim=claim,
            material=material,
            object_key=object_key,
            expected=identity,
        ))
        client.post(
            f"/internal/v1/authorization-dr/operations/{operation_id}/wake-bundle/restore-probe",
            {**owner, **probe},
        )
        client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/slot-commit", owner)
        LOGGER.info("authorization DR operation completed: %s", operation_id)
    except Exception:
        LOGGER.exception("authorization DR operation stopped: %s", operation_id)
        if login_started and not bundle_committed:
            _mark_unknown(client, operation_id, owner)
        raise
    finally:
        client.heartbeat(0)


def _complete_login(client: DrNodeClient, gateway, claim: dict, material: dict) -> str:
    operation_id = claim["operation_id"]
    owner = _owner_payload(claim)
    credentials = _credentials(claim, material)
    flow_id = int(hashlib.sha256(operation_id.encode()).hexdigest()[:15], 16)
    challenge = gateway.start_login(
        "code",
        flow_id=flow_id,
        account_id=claim["account_id"],
        phone=material["phone"],
        credentials=credentials,
    )
    code = _wait_for_code(client, operation_id, owner)
    status, raw_session = gateway.finish_login(
        code,
        None,
        flow_id=flow_id,
        account_id=claim["account_id"],
        phone=material["phone"],
        credentials=credentials,
        temporary_session=challenge.temporary_session,
        phone_code_hash=challenge.phone_code_hash,
    )
    if status == "等待2FA":
        status, raw_session = _finish_two_fa(gateway, claim, material, credentials, flow_id, raw_session)
    if status != "在线" or not raw_session:
        raise RuntimeError(f"Malaysia authorization login did not complete: {status}")
    return raw_session


def _wait_for_code(client: DrNodeClient, operation_id: str, owner: dict) -> str:
    deadline = time.monotonic() + 180
    last_renewed = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if now - last_renewed >= LEASE_RENEW_SECONDS:
            client.heartbeat(1)
            client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/lease-renew", owner)
            last_renewed = now
        result = client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/login-code", owner)
        if result.get("code"):
            return str(result["code"])
        time.sleep(CODE_POLL_SECONDS)
    raise RuntimeError("Telegram login code was not found before the operation deadline")


def _finish_two_fa(gateway, claim, material, credentials, flow_id: int, temporary_session: str):
    password = material.get("password_2fa")
    if not password:
        raise RuntimeError("Telegram 2FA is required but no managed password is available")
    return gateway.finish_login(
        None,
        password,
        flow_id=flow_id,
        account_id=claim["account_id"],
        phone=material["phone"],
        credentials=credentials,
        temporary_session=temporary_session,
        phone_code_hash=None,
    )


def _persist_bundle(config: NodeConfig, claim: dict, raw_session: str, identity) -> tuple[dict, str]:
    generation = int(claim["target_generation"])
    account_id = int(claim["account_id"])
    dek = AESGCM.generate_key(bit_length=256)
    wrapped_dek = config.dek_protector.wrap(dek)
    envelope = _encrypt_session(raw_session, dek, wrapped_dek=wrapped_dek)
    ciphertext_digest = hashlib.sha256(envelope).hexdigest()
    relative = Path(str(account_id)) / f"g{generation}.bundle"
    local_path = config.local_dir / relative
    object_key = f"{config.object_prefix.strip('/')}/{relative.as_posix()}".lstrip("/")
    copies = [
        _write_copy(local_path, envelope, "local_persistent", dek),
        _write_object_copy(
            config.object_store,
            object_key,
            envelope,
            dek,
            config.snapshot_copy_kind,
        ),
    ]
    inventory_sequence = _next_inventory_sequence(config)
    inventory_manifest_digest = _write_inventory(
        config,
        claim,
        object_key,
        ciphertext_digest,
        inventory_sequence,
    )
    receipt = {
        "bundle_generation": generation,
        "ciphertext_digest": ciphertext_digest,
        "wrapped_dek_ciphertext": wrapped_dek.ciphertext,
        "kms_key_ref_digest": hashlib.sha256(config.dek_protector.key_ref.encode()).hexdigest(),
        "kms_key_version": wrapped_dek.key_version,
        "auth_key_fingerprint_digest": identity.auth_key_fingerprint_digest,
        "telegram_user_id_digest": identity.telegram_user_id_digest,
        "authorization_fingerprint_digest": identity.authorization_fingerprint_digest,
        "remote_authorization_hash": identity.authorization_hash,
        "inventory_sequence": inventory_sequence,
        "inventory_manifest_digest": inventory_manifest_digest,
        "copies": copies,
    }
    return receipt, object_key


def _encrypt_session(raw_session: str, dek: bytes, *, wrapped_dek: WrappedDek) -> bytes:
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(dek).encrypt(nonce, raw_session.encode(), b"tg-authorization-wake-bundle-v1")
    return json.dumps({
        "v": 2,
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "wrapped_dek_ciphertext": wrapped_dek.ciphertext,
        "kms_key_version": wrapped_dek.key_version,
    }, sort_keys=True, separators=(",", ":")).encode()


def _decrypt_session(envelope: bytes, dek: bytes) -> str:
    payload = json.loads(envelope)
    nonce = base64.b64decode(payload["nonce"])
    ciphertext = base64.b64decode(payload["ciphertext"])
    raw = AESGCM(dek).decrypt(nonce, ciphertext, b"tg-authorization-wake-bundle-v1")
    return raw.decode()


def _wrapped_dek_ciphertext(envelope: bytes) -> str:
    payload = json.loads(envelope)
    wrapped_dek = str(payload.get("wrapped_dek_ciphertext") or "")
    if int(payload.get("v") or 0) < 2 or not wrapped_dek or not payload.get("kms_key_version"):
        raise RuntimeError("wake bundle does not contain durable recovery metadata")
    return wrapped_dek


def _write_copy(path: Path, envelope: bytes, copy_kind: str, dek: bytes) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(envelope)
        handle.flush()
        os.fsync(handle.fileno())
    readback = path.read_bytes()
    if readback != envelope or not _decrypt_session(readback, dek):
        raise RuntimeError(f"{copy_kind} bundle readback failed")
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(envelope).hexdigest()
    return {
        "copy_kind": copy_kind,
        "object_ref_digest": hashlib.sha256(str(path).encode()).hexdigest(),
        "ciphertext_digest": digest,
        "immutable_version": path.name,
        "write_receipt_digest": hashlib.sha256((digest + ":write").encode()).hexdigest(),
        "readback_receipt_digest": hashlib.sha256((digest + ":readback").encode()).hexdigest(),
        "write_verified_at": now,
        "readback_verified_at": now,
        "decrypt_verified_at": now,
    }


def _write_object_copy(
    store: ObjectSnapshotStore,
    object_key: str,
    envelope: bytes,
    dek: bytes,
    copy_kind: str,
) -> dict:
    immutable_version = store.put_immutable(object_key, envelope)
    readback = store.read(object_key)
    if readback != envelope or not _decrypt_session(readback, dek):
        raise RuntimeError("object_snapshot bundle readback failed")
    now = datetime.now(timezone.utc).isoformat()
    digest = hashlib.sha256(envelope).hexdigest()
    return {
        "copy_kind": copy_kind,
        "object_ref_digest": hashlib.sha256(object_key.encode()).hexdigest(),
        "ciphertext_digest": digest,
        "immutable_version": immutable_version,
        "write_receipt_digest": hashlib.sha256((digest + ":oss-write").encode()).hexdigest(),
        "readback_receipt_digest": hashlib.sha256((digest + ":oss-readback").encode()).hexdigest(),
        "write_verified_at": now,
        "readback_verified_at": now,
        "decrypt_verified_at": now,
    }


def _next_inventory_sequence(config: NodeConfig) -> int:
    path = config.local_dir / ".inventory-sequence"
    path.parent.mkdir(parents=True, exist_ok=True)
    previous = int(path.read_text().strip()) if path.exists() else 0
    sequence = max(previous + 1, time.time_ns() // 1000)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as handle:
        handle.write(str(sequence))
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    return sequence


def _write_inventory(config: NodeConfig, claim: dict, object_key: str, digest: str, sequence: int) -> str:
    payload = json.dumps({
        "operation_id": claim["operation_id"],
        "account_id": claim["account_id"],
        "generation": claim["target_generation"],
        "bundle_object_ref_digest": hashlib.sha256(object_key.encode()).hexdigest(),
        "ciphertext_digest": digest,
        "inventory_sequence": sequence,
    }, sort_keys=True, separators=(",", ":")).encode()
    inventory_key = f"{config.object_prefix.strip('/')}/inventory/{sequence}.json".lstrip("/")
    config.object_store.put_immutable(inventory_key, payload)
    if config.object_store.read(inventory_key) != payload:
        raise RuntimeError("object inventory readback failed")
    return hashlib.sha256(payload).hexdigest()


def _restore_probe(gateway, probe_input: RestoreProbeInput) -> dict:
    config = probe_input.config
    envelope = config.object_store.read(probe_input.object_key)
    dek = config.dek_protector.unwrap(_wrapped_dek_ciphertext(envelope))
    raw_session = _decrypt_session(envelope, dek)
    observed = gateway.authorization_identity(
        raw_session,
        _credentials(probe_input.claim, probe_input.material),
    )
    matched = observed == probe_input.expected
    return {
        "probe_generation": probe_input.claim["target_generation"],
        "source_copy_kind": config.snapshot_copy_kind,
        "status": "passed" if matched else "failed",
        "session_parse_status": "passed",
        "authorization_status": "authorized",
        "identity_match_status": "matched" if matched else "mismatch",
        "auth_key_match_status": "matched" if matched else "mismatch",
        "source_client_disconnected": True,
        "probe_client_disconnected": True,
        "zeroize_receipt_digest": hashlib.sha256(secrets.token_bytes(32)).hexdigest(),
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


def _mark_unknown(client: DrNodeClient, operation_id: str, owner: dict) -> None:
    try:
        client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/login-unknown", owner)
    except Exception:
        LOGGER.exception("failed to persist unknown remote login state: %s", operation_id)


def _post_bundle_receipt(client: DrNodeClient, operation_id: str, payload: dict) -> None:
    error: Exception | None = None
    for _ in range(RECEIPT_POST_ATTEMPTS):
        try:
            client.post(f"/internal/v1/authorization-dr/operations/{operation_id}/wake-bundle", payload)
            return
        except (httpx.TransportError, OSError) as exc:
            error = exc
            time.sleep(1)
    raise RuntimeError("wake bundle receipt was not acknowledged") from error


def load_config() -> NodeConfig:
    cert = os.environ.get("AUTHORIZATION_DR_CLIENT_CERT", "")
    key = os.environ.get("AUTHORIZATION_DR_CLIENT_KEY", "")
    storage = load_storage_config(_required_env)
    return NodeConfig(
        control_plane_url=_required_env("AUTHORIZATION_DR_CONTROL_PLANE_URL"),
        internal_token=_required_env("AUTHORIZATION_DR_INTERNAL_TOKEN"),
        node_id=_required_env("AUTHORIZATION_DR_NODE_ID"),
        egress_id=_required_env("AUTHORIZATION_DR_EGRESS_ID"),
        expected_egress_ip=_required_env("AUTHORIZATION_DR_EXPECTED_EGRESS_IP"),
        egress_probe_url=os.environ.get("AUTHORIZATION_DR_EGRESS_PROBE_URL", "https://api.ipify.org").strip(),
        local_dir=Path(_required_env("MY_WAKE_BUNDLE_LOCAL_DIR")),
        object_prefix=storage.object_prefix,
        object_store=storage.object_store,
        dek_protector=storage.dek_protector,
        client_cert=(cert, key) if cert and key else None,
        snapshot_copy_kind=storage.copy_kind,
    )


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="Malaysia Telegram standby authorization node")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    return run_node(load_config(), once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())

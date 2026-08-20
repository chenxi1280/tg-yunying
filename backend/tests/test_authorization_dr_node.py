from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.integrations.telegram import AuthorizationIdentity
from app.workers.authorization_dr_node import (
    NodeConfig,
    RestoreProbeInput,
    _decrypt_session,
    _persist_bundle,
    _restore_probe,
    _verify_expected_egress,
    _wrapped_dek_ciphertext,
)
from app.workers.authorization_dr_kms import AlibabaKmsDekProtector, WrappedDek
from app.workers.authorization_dr_ssh import FileDekProtector, SshMirrorObjectSnapshotStore


pytestmark = pytest.mark.no_postgres


class MemoryObjectStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_immutable(self, object_key: str, payload: bytes) -> str:
        if object_key in self.objects:
            raise FileExistsError(object_key)
        self.objects[object_key] = payload
        return "object-version-1"

    def read(self, object_key: str) -> bytes:
        return self.objects[object_key]


class IdentityGateway:
    def __init__(self, identity: AuthorizationIdentity):
        self.identity = identity

    def authorization_identity(self, raw_session, credentials):
        assert raw_session == "raw-session-value"
        assert credentials.api_id == 1003
        return self.identity


class MemoryDekProtector:
    key_ref = "kms-test-key"

    def wrap(self, plaintext: bytes) -> WrappedDek:
        return WrappedDek(base64.b64encode(plaintext).decode(), "kms-v1")

    def unwrap(self, ciphertext: str) -> bytes:
        return base64.b64decode(ciphertext)


class FakeKmsClient:
    def __init__(self):
        self.encrypt_request = None
        self.decrypt_request = None

    def encrypt(self, request):
        from alibabacloud_kms20160120 import models

        self.encrypt_request = request
        body = models.EncryptResponseBody(ciphertext_blob="kms-ciphertext", key_version_id="key-v3")
        return SimpleNamespace(body=body)

    def decrypt(self, request):
        from alibabacloud_kms20160120 import models

        self.decrypt_request = request
        body = models.DecryptResponseBody(plaintext=base64.b64encode(b"d" * 32).decode())
        return SimpleNamespace(body=body)


def test_alibaba_kms_protector_uses_key_version_and_matching_context() -> None:
    client = FakeKmsClient()
    protector = AlibabaKmsDekProtector(
        endpoint="kms.ap-southeast-3.aliyuncs.com",
        region_id="ap-southeast-3",
        access_key_id="test-id",
        access_key_secret="test-secret",
        key_id="key-malaysia",
        client=client,
    )

    wrapped = protector.wrap(b"d" * 32)
    plaintext = protector.unwrap(wrapped.ciphertext)

    assert wrapped == WrappedDek("kms-ciphertext", "key-v3")
    assert plaintext == b"d" * 32
    assert client.encrypt_request.key_id == "key-malaysia"
    assert client.encrypt_request.encryption_context == client.decrypt_request.encryption_context


def test_file_dek_protector_round_trip(tmp_path: Path) -> None:
    key_file = tmp_path / "recovery.key"
    key_file.write_text(base64.b64encode(b"k" * 32).decode())
    protector = FileDekProtector(str(key_file))

    wrapped = protector.wrap(b"d" * 32)

    assert wrapped.key_version.startswith("ssh-key-")
    assert protector.unwrap(wrapped.ciphertext) == b"d" * 32


def test_ssh_mirror_writes_create_only_and_reads_back() -> None:
    payload = b"encrypted-session-envelope"
    calls = []

    def runner(args, **kwargs):
        calls.append((args, kwargs))
        stdout = payload if "cat --" in args[-1] else hashlib.sha256(payload).hexdigest().encode()
        return SimpleNamespace(returncode=0, stdout=stdout, stderr=b"")

    store = SshMirrorObjectSnapshotStore(
        host="198.51.100.8",
        port=22,
        user="dr-mirror",
        identity_file="/run/secrets/id_ed25519",
        known_hosts_file="/run/secrets/known_hosts",
        remote_dir="/srv/dr",
        runner=runner,
    )

    version = store.put_immutable("bundle/101/g2.bundle", payload)

    assert version == f"sha256-{hashlib.sha256(payload).hexdigest()}"
    assert store.read("bundle/101/g2.bundle") == payload
    assert calls[0][1]["input"] == payload
    assert 'test ! -e "$target"' in calls[0][0][-1]
    with pytest.raises(ValueError, match="normalized"):
        store.read("../outside")


def test_malaysia_deploy_checks_ssh_mirror_access_before_start() -> None:
    script_path = Path(__file__).parents[2] / "deploy/malaysia/deploy-authorization-dr-node.sh"
    script = script_path.read_text()

    access_check = script.index('printf -v remote_access_check')
    ssh_check = script.index('"$remote_access_check"')
    container_start = script.index('docker compose -f "$COMPOSE_FILE" up')

    assert access_check < ssh_check < container_start
    assert 'test -d %q && test -x %q && test -w %q' in script
    assert "StrictHostKeyChecking=yes" in script


def test_wake_bundle_has_two_readable_immutable_copies(tmp_path: Path) -> None:
    object_store = MemoryObjectStore()
    config = NodeConfig(
        control_plane_url="https://control.invalid",
        internal_token="token",
        node_id="my-node-1",
        egress_id="my-egress-1",
        expected_egress_ip="203.0.113.8",
        egress_probe_url="https://egress.invalid",
        local_dir=tmp_path / "local",
        object_prefix="dr-test",
        object_store=object_store,
        dek_protector=MemoryDekProtector(),
        client_cert=None,
    )
    claim = {
        "operation_id": "operation-1",
        "account_id": 101,
        "target_generation": 5,
        "developer_app_id": 3,
    }
    identity = AuthorizationIdentity("12345", "a" * 64, "b" * 64, "1" * 64)

    receipt, object_key = _persist_bundle(config, claim, "raw-session-value", identity)

    assert {item["copy_kind"] for item in receipt["copies"]} == {"local_persistent", "remote_ssh_snapshot"}
    envelope = object_store.read(object_key)
    assert _wrapped_dek_ciphertext(envelope) == receipt["wrapped_dek_ciphertext"]
    dek = config.dek_protector.unwrap(_wrapped_dek_ciphertext(envelope))
    assert _decrypt_session(envelope, dek) == "raw-session-value"
    with pytest.raises(FileExistsError):
        _persist_bundle(config, claim, "raw-session-value", identity)

    second_claim = {**claim, "operation_id": "operation-1b", "account_id": 103}
    second_receipt, _ = _persist_bundle(config, second_claim, "raw-session-value", identity)
    assert second_receipt["inventory_sequence"] > receipt["inventory_sequence"]
    assert any("/inventory/" in key for key in object_store.objects)


def test_restore_probe_reads_snapshot_and_matches_authorization(tmp_path: Path) -> None:
    object_store = MemoryObjectStore()
    config = NodeConfig(
        control_plane_url="https://control.invalid",
        internal_token="token",
        node_id="my-node-1",
        egress_id="my-egress-1",
        expected_egress_ip="203.0.113.8",
        egress_probe_url="https://egress.invalid",
        local_dir=tmp_path / "local",
        object_prefix="dr-test",
        object_store=object_store,
        dek_protector=MemoryDekProtector(),
        client_cert=None,
    )
    claim = {
        "operation_id": "operation-2",
        "account_id": 102,
        "target_generation": 3,
        "developer_app_id": 3,
    }
    material = {
        "api_id": 1003,
        "api_hash": "hash",
        "credentials_version": 1,
        "app_name": "App C",
    }
    identity = AuthorizationIdentity("67890", "c" * 64, "d" * 64, "2" * 64)
    receipt, object_key = _persist_bundle(config, claim, "raw-session-value", identity)

    probe = _restore_probe(IdentityGateway(identity), RestoreProbeInput(
        config=config,
        claim=claim,
        material=material,
        object_key=object_key,
        expected=identity,
    ))

    assert probe["status"] == "passed"
    assert probe["source_copy_kind"] == "remote_ssh_snapshot"
    assert probe["source_client_disconnected"] is True
    assert probe["probe_client_disconnected"] is True


def test_node_rejects_unexpected_egress(tmp_path: Path, monkeypatch) -> None:
    config = NodeConfig(
        control_plane_url="https://control.invalid",
        internal_token="token",
        node_id="my-node-1",
        egress_id="my-egress-1",
        expected_egress_ip="203.0.113.8",
        egress_probe_url="https://egress.invalid",
        local_dir=tmp_path / "local",
        object_prefix="dr-test",
        object_store=MemoryObjectStore(),
        dek_protector=MemoryDekProtector(),
        client_cert=None,
    )
    monkeypatch.setattr(
        "app.workers.authorization_dr_node.httpx.get",
        lambda *_args, **_kwargs: type("Response", (), {"text": "203.0.113.9"})(),
    )

    with pytest.raises(RuntimeError, match="fixed IP"):
        _verify_expected_egress(config)

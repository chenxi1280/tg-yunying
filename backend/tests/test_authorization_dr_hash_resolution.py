from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.security import decrypt_secret, encrypt_secret
from app.services.authorization_dr import WakeBundleReceipt
from app.services.authorization_dr import wake_bundle


pytestmark = pytest.mark.no_postgres


def _receipt(remote_hash: str) -> WakeBundleReceipt:
    return WakeBundleReceipt(
        bundle_generation=2,
        ciphertext_digest="a" * 64,
        wrapped_dek_ciphertext="wrapped",
        kms_key_ref_digest="b" * 64,
        kms_key_version="file-key-v1",
        auth_key_fingerprint_digest="c" * 64,
        telegram_user_id_digest="d" * 64,
        authorization_fingerprint_digest="e" * 64,
        remote_authorization_hash_ciphertext=encrypt_secret(remote_hash),
        inventory_sequence=1,
        inventory_manifest_digest="f" * 64,
        copies=(),
    )


def test_zero_current_hash_is_resolved_from_silicon_valley_peer(monkeypatch) -> None:
    operation = SimpleNamespace(account_id=24, source_authorization_id=46)
    monkeypatch.setattr(wake_bundle, "resolve_peer_authorization_hash", lambda *args, **kwargs: "peer-hash")

    resolved = wake_bundle._resolve_remote_authorization_hash(object(), operation, _receipt("0"))

    assert decrypt_secret(resolved.remote_authorization_hash_ciphertext) == "peer-hash"


def test_nonzero_hash_does_not_query_peer_sessions(monkeypatch) -> None:
    monkeypatch.setattr(
        wake_bundle,
        "resolve_peer_authorization_hash",
        lambda *args, **kwargs: pytest.fail("peer lookup must not run"),
    )

    resolved = wake_bundle._resolve_remote_authorization_hash(
        object(),
        SimpleNamespace(account_id=24, source_authorization_id=46),
        _receipt("direct-hash"),
    )

    assert decrypt_secret(resolved.remote_authorization_hash_ciphertext) == "direct-hash"

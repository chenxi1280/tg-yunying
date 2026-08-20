from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.security import decrypt_secret, encrypt_secret
from app.services import account_authorization_metadata
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


def test_peer_lookup_prefers_canonical_authorization_over_stale_account_session(monkeypatch) -> None:
    account = SimpleNamespace(id=87, session_ciphertext="stale-account-session")
    authorization = SimpleNamespace(
        developer_app_id=2,
        proxy_id=16,
        session_ciphertext="current-standby-session",
    )
    app = SimpleNamespace(id=2)
    proxy = SimpleNamespace(id=16)

    class FakeSession:
        def get(self, model, object_id):
            return app if object_id == 2 else proxy

    monkeypatch.setattr(
        account_authorization_metadata,
        "_peer_authorization_rows",
        lambda *_args: [authorization],
    )
    monkeypatch.setattr(
        account_authorization_metadata,
        "credentials_for_developer_app",
        lambda observed_app, observed_proxy: (observed_app.id, observed_proxy.id),
    )
    monkeypatch.setattr(
        account_authorization_metadata,
        "credentials_for_account",
        lambda *_args, **_kwargs: pytest.fail("stale account projection must not be used"),
    )
    calls = []
    monkeypatch.setattr(
        account_authorization_metadata.gateway,
        "list_authorizations",
        lambda raw_session, credentials: calls.append((raw_session, credentials)) or [],
    )

    views = list(account_authorization_metadata._peer_authorization_views(FakeSession(), account, 162))

    assert views == [[]]
    assert calls == [("current-standby-session", (2, 16))]

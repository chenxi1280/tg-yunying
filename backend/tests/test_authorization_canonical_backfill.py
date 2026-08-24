from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import AuthorizationIdentity
from app.models import AuditLog, TelegramDeveloperApp, Tenant, TgAccount, TgAccountAuthorization
from app.services import authorization_canonical_backfill as backfill


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="tenant"))
    session.add(
        TelegramDeveloperApp(
            id=10,
            app_name="A",
            api_id=100,
            api_hash_ciphertext="encrypted",
            is_active=True,
        )
    )
    session.commit()
    return session


def _account(account_id: int, *, session_ciphertext: str | None = "session") -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        status="在线",
        developer_app_id=10,
        proxy_id=20,
        session_ciphertext=session_ciphertext,
        authorization_generation=3,
        authorization_fact_generation=4,
        connection_generation=5,
    )


def test_preview_and_apply_backfill_preserve_primary_session_and_generations(monkeypatch) -> None:
    with _session() as session:
        session.add_all([_account(1), _account(2, session_ciphertext=None)])
        session.commit()
        monkeypatch.setattr(backfill, "_auth_key_digest", lambda value: "a" * 64)

        preview = backfill.preview_canonical_authorization_backfill(session, 1)
        result = backfill.apply_canonical_authorization_backfill(
            session,
            1,
            expected_fingerprint=preview["fingerprint"],
            requested_by="requester",
            approved_by="approver",
            approval_ref="approved-change",
        )

        account = session.get(TgAccount, 1)
        row = session.get(TgAccountAuthorization, account.current_authorization_id)
        assert preview["counts"] == {"eligible": 1, "missing_session": 1}
        assert result["created_count"] == 1
        assert result["readback"]["counts"] == {"already_canonical": 1, "missing_session": 1}
        assert (account.session_ciphertext, account.authorization_generation) == ("session", 3)
        assert (account.authorization_fact_generation, account.connection_generation) == (4, 5)
        assert row.logical_slot == "primary"
        assert row.is_current is True
        assert row.provision_region_code == "sv"
        assert row.auth_key_fingerprint_digest == "a" * 64


def test_apply_rejects_changed_preview_fingerprint(monkeypatch) -> None:
    with _session() as session:
        account = _account(1)
        session.add(account)
        session.commit()
        monkeypatch.setattr(backfill, "_auth_key_digest", lambda value: "a" * 64)
        fingerprint = backfill.preview_canonical_authorization_backfill(session, 1)["fingerprint"]
        account.connection_generation += 1
        session.commit()

        with pytest.raises(ValueError, match="fingerprint changed"):
            backfill.apply_canonical_authorization_backfill(
                session,
                1,
                expected_fingerprint=fingerprint,
                requested_by="requester",
                approved_by="approver",
                approval_ref="approved-change",
            )


def test_preview_classifies_corrupt_encrypted_session() -> None:
    with _session() as session:
        session.add(_account(1, session_ciphertext="enc:v2:not-valid"))
        session.commit()

        preview = backfill.preview_canonical_authorization_backfill(session, 1)

        assert preview["counts"] == {"session_unreadable": 1}


def test_apply_links_matching_existing_current_without_creating_duplicate(monkeypatch) -> None:
    with _session() as session:
        account = _account(1)
        session.add(account)
        session.flush()
        existing = TgAccountAuthorization(
            tenant_id=1,
            account_id=1,
            role="primary",
            logical_slot="primary",
            is_slot_current=True,
            provision_region_code="sv",
            developer_app_id=10,
            proxy_id=20,
            session_ciphertext="session",
            status="active",
            health_status="healthy",
            is_current=True,
        )
        session.add(existing)
        session.commit()
        monkeypatch.setattr(backfill, "_auth_key_digest", lambda value: "a" * 64)

        preview = backfill.preview_canonical_authorization_backfill(session, 1)
        result = backfill.apply_canonical_authorization_backfill(
            session,
            1,
            expected_fingerprint=preview["fingerprint"],
            requested_by="requester",
            approved_by="approver",
            approval_ref="link-approved",
        )

        account = session.get(TgAccount, 1)
        assert preview["counts"] == {"link_existing": 1}
        assert result["created_count"] == 0
        assert result["linked_count"] == 1
        assert account.current_authorization_id == existing.id
        assert session.query(TgAccountAuthorization).count() == 1
        assert (account.authorization_generation, account.connection_generation) == (3, 5)


def test_apply_requires_distinct_requester_and_approver(monkeypatch) -> None:
    with _session() as session:
        session.add(_account(1))
        session.commit()
        monkeypatch.setattr(backfill, "_auth_key_digest", lambda value: "a" * 64)
        fingerprint = backfill.preview_canonical_authorization_backfill(session, 1)["fingerprint"]

        with pytest.raises(ValueError, match="must differ"):
            backfill.apply_canonical_authorization_backfill(
                session,
                1,
                expected_fingerprint=fingerprint,
                requested_by="same",
                approved_by="same",
                approval_ref="approved-change",
            )


def test_primary_qualification_updates_only_identity_facts(monkeypatch) -> None:
    with _session() as session:
        session.add(_account(1))
        session.commit()
        monkeypatch.setattr(backfill, "_auth_key_digest", lambda value: "a" * 64)
        fingerprint = backfill.preview_canonical_authorization_backfill(session, 1)["fingerprint"]
        backfill.apply_canonical_authorization_backfill(
            session,
            1,
            expected_fingerprint=fingerprint,
            requested_by="requester",
            approved_by="approver",
            approval_ref="backfill-approved",
        )
        account = session.get(TgAccount, 1)
        activated = session.get(TgAccountAuthorization, account.current_authorization_id)
        activated.logical_slot = "standby_1"
        activated.role = "standby_1"
        session.commit()
        preview = backfill.preview_primary_qualification(session, 1, 1)
        monkeypatch.setattr(
            backfill.gateway,
            "authorization_identity",
            lambda *args, **kwargs: AuthorizationIdentity(
                authorization_hash="0",
                auth_key_fingerprint_digest="a" * 64,
                telegram_user_id_digest="b" * 64,
                authorization_fingerprint_digest="c" * 64,
            ),
        )
        monkeypatch.setattr(
            backfill,
            "resolve_authorization_identity_hash",
            lambda _session, _account_id, identity, **_kwargs: (
                AuthorizationIdentity(
                    authorization_hash="123",
                    auth_key_fingerprint_digest=identity.auth_key_fingerprint_digest,
                    telegram_user_id_digest=identity.telegram_user_id_digest,
                    authorization_fingerprint_digest=identity.authorization_fingerprint_digest,
                ),
                "peer_observer",
            ),
        )

        result = backfill.qualify_primary_authorization(
            session,
            1,
            1,
            expected_fingerprint=preview["fingerprint"],
            actor="approver",
            approval_ref="qualify-approved",
        )

        account = session.get(TgAccount, 1)
        row = session.get(TgAccountAuthorization, account.current_authorization_id)
        assert result["primary_unchanged"] is True
        assert result["authorization_hash_source"] == "peer_observer"
        assert account.session_ciphertext == "session"
        assert (account.authorization_generation, account.connection_generation) == (3, 5)
        assert account.authorization_fact_generation == 5
        assert row.telegram_user_id_digest == "b" * 64
        assert row.fact_version == 2
        assert row.logical_slot == "standby_1"


def test_primary_qualification_probe_failure_writes_nothing(monkeypatch) -> None:
    with _session() as session:
        session.add(_account(1))
        session.commit()
        monkeypatch.setattr(backfill, "_auth_key_digest", lambda value: "a" * 64)
        canonical = backfill.preview_canonical_authorization_backfill(session, 1)
        backfill.apply_canonical_authorization_backfill(
            session,
            1,
            expected_fingerprint=canonical["fingerprint"],
            requested_by="requester",
            approved_by="approver",
            approval_ref="backfill-approved",
        )
        account = session.get(TgAccount, 1)
        primary = session.get(TgAccountAuthorization, account.current_authorization_id)
        primary.health_status = "legacy"
        session.commit()
        preview = backfill.preview_primary_qualification(session, 1, 1)
        before = _primary_snapshot(account, primary)
        audit_count = session.query(AuditLog).count()

        def reject_probe(*_args, **_kwargs):
            raise RuntimeError("identity probe failed")

        monkeypatch.setattr(backfill.gateway, "authorization_identity", reject_probe)

        with pytest.raises(RuntimeError, match="identity probe failed"):
            backfill.qualify_primary_authorization(
                session,
                1,
                1,
                expected_fingerprint=preview["fingerprint"],
                actor="approver",
                approval_ref="qualify-approved",
            )

        session.expire_all()
        account = session.get(TgAccount, 1)
        primary = session.get(TgAccountAuthorization, account.current_authorization_id)
        assert _primary_snapshot(account, primary) == before
        assert session.query(AuditLog).count() == audit_count


def _primary_snapshot(account, primary) -> tuple:
    return (
        account.current_authorization_id,
        account.session_ciphertext,
        account.developer_app_id,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.session_ciphertext,
        primary.status,
        primary.health_status,
        primary.telegram_user_id_digest,
        primary.auth_key_fingerprint_digest,
        primary.telegram_authorization_hash_ciphertext,
        primary.fact_version,
        primary.last_authoritative_error_code,
        primary.disabled_at,
    )

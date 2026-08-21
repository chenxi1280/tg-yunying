from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import TelegramDeveloperApp, Tenant, TgAccount, TgAccountAuthorization
from app.services.authorization_dr import apply_sv_redundancy_repair, preview_sv_redundancy_repair


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="tenant"))
    session.add_all([
        TelegramDeveloperApp(id=1, app_name="A", api_id=101, api_hash_ciphertext="a"),
        TelegramDeveloperApp(id=2, app_name="B", api_id=102, api_hash_ciphertext="b"),
        TelegramDeveloperApp(id=3, app_name="C", api_id=103, api_hash_ciphertext="c"),
    ])
    account = TgAccount(
        id=11,
        tenant_id=1,
        display_name="account",
        phone_masked="11",
        developer_app_id=2,
        session_ciphertext="primary-session",
    )
    session.add(account)
    session.flush()
    session.add_all([
        TgAccountAuthorization(
            id=21,
            tenant_id=1,
            account_id=11,
            role="standby_repair",
            logical_slot="standby_repair",
            developer_app_id=1,
            session_ciphertext="repair-session",
            status="needs_repair",
            fact_version=4,
        ),
        TgAccountAuthorization(
            id=22,
            tenant_id=1,
            account_id=11,
            role="standby_2",
            logical_slot="standby_2",
            developer_app_id=3,
            session_ciphertext="standby-2-session",
            status="standby",
        ),
    ])
    session.commit()
    return session


def test_repair_promotes_only_after_matching_remote_identity(monkeypatch) -> None:
    with _session() as session:
        preview = preview_sv_redundancy_repair(session, 1, [11])
        account_credential_calls: list[bool] = []
        identities = iter([
            SimpleNamespace(authorization_hash="primary", auth_key_fingerprint_digest="1" * 64, telegram_user_id_digest="3" * 64),
            SimpleNamespace(authorization_hash="repair", auth_key_fingerprint_digest="2" * 64, telegram_user_id_digest="3" * 64),
        ])
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_redundancy.credentials_for_account",
            lambda *_args, use_proxy=False, **_kwargs: account_credential_calls.append(use_proxy) or SimpleNamespace(),
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_redundancy.gateway.authorization_identity",
            lambda *_args, **_kwargs: next(identities),
        )

        result = apply_sv_redundancy_repair(
            session,
            1,
            [11],
            expected_fingerprint=preview["target_set_fingerprint"],
            actor="operator",
            approval_ref="OPS-DR-REPAIR",
        )
        repaired = session.get(TgAccountAuthorization, 21)

        assert result["succeeded_count"] == 1
        assert repaired.role == "standby_1"
        assert repaired.logical_slot == "standby_1"
        assert repaired.status == "standby"
        assert repaired.health_status == "healthy"
        assert repaired.auth_key_fingerprint_digest == "2" * 64
        assert repaired.fact_version == 5
        assert account_credential_calls == [True]


def test_repair_keeps_row_unchanged_when_remote_identity_differs(monkeypatch) -> None:
    with _session() as session:
        preview = preview_sv_redundancy_repair(session, 1, [11])
        identities = iter([
            SimpleNamespace(authorization_hash="primary", auth_key_fingerprint_digest="1" * 64, telegram_user_id_digest="3" * 64),
            SimpleNamespace(authorization_hash="repair", auth_key_fingerprint_digest="2" * 64, telegram_user_id_digest="4" * 64),
        ])
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_redundancy.gateway.authorization_identity",
            lambda *_args, **_kwargs: next(identities),
        )

        result = apply_sv_redundancy_repair(
            session,
            1,
            [11],
            expected_fingerprint=preview["target_set_fingerprint"],
            actor="operator",
            approval_ref="OPS-DR-REPAIR",
        )
        repaired = session.get(TgAccountAuthorization, 21)

        assert result["failed_count"] == 1
        assert repaired.role == "standby_repair"
        assert repaired.fact_version == 4


def test_repair_atomically_retains_duplicate_app_standby_and_promotes_distinct_repair(monkeypatch) -> None:
    with _session() as session:
        duplicate = TgAccountAuthorization(
            id=23, tenant_id=1, account_id=11, role="standby_1", logical_slot="standby_1",
            developer_app_id=2, session_ciphertext="duplicate-app-session", status="standby",
            health_status="healthy", is_slot_current=True, protected_from_cleanup=True, fact_version=2,
        )
        session.add(duplicate)
        session.commit()
        preview = preview_sv_redundancy_repair(session, 1, [11])
        identities = iter([
            SimpleNamespace(authorization_hash="primary", auth_key_fingerprint_digest="1" * 64, telegram_user_id_digest="3" * 64),
            SimpleNamespace(authorization_hash="repair", auth_key_fingerprint_digest="2" * 64, telegram_user_id_digest="3" * 64),
        ])
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_redundancy.gateway.authorization_identity",
            lambda *_args, **_kwargs: next(identities),
        )

        result = apply_sv_redundancy_repair(
            session, 1, [11], expected_fingerprint=preview["target_set_fingerprint"],
            actor="operator", approval_ref="OPS-DUPLICATE-APP-REPAIR",
        )
        repaired = session.get(TgAccountAuthorization, 21)
        retained = session.get(TgAccountAuthorization, 23)

        assert result["succeeded_count"] == 1
        assert (repaired.role, repaired.logical_slot, repaired.developer_app_id) == ("standby_1", "standby_1", 1)
        assert (retained.role, retained.logical_slot, retained.developer_app_id) == (
            "standby_repair", "standby_repair", 2,
        )
        assert retained.status == "needs_repair"
        assert retained.protected_from_cleanup is True

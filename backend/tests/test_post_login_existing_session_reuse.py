from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgLoginFlow,
)
from app.services._common import _now
from app.services.authorization_dr import apply_abc_backup, preview_abc_backup
from tests.test_authorization_abc_backup import session  # noqa: F401


pytestmark = pytest.mark.no_postgres
ABC_BACKUP_MODULE = "app.services.authorization_dr.abc_backup"
CURRENT_PASSWORD = "telegram-accepted-current"


class ExistingSessionHarness:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def start_login(self, db, account_id, **kwargs):
        self.calls.append(("start", account_id, kwargs["method"]))
        flow = TgLoginFlow(
            tenant_id=1,
            account_id=account_id,
            method="code",
            status="等待验证码",
            authorization_role="standby_1",
            developer_app_id=2,
            challenge_sent_at=_now(),
            code_expires_at=_now().replace(year=_now().year + 1),
        )
        db.add(flow)
        db.commit()
        return flow

    def poll_code(self, account_id, *, session_ciphertext, credentials):
        self.calls.append(("poll", account_id, session_ciphertext, credentials.app_id))
        return [SimpleNamespace(
            code="12345",
            message_id="777000:post-login",
            received_at=_now(),
        )]

    def verify_login(self, db, account_id, *_args, **kwargs):
        self.calls.append(("verify", account_id, kwargs["password_2fa"]))
        asset = TgAccountAuthorization(
            tenant_id=1,
            account_id=account_id,
            role="standby_1",
            logical_slot="standby_1",
            developer_app_id=2,
            developer_app_api_id_snapshot=1002,
            session_ciphertext="independent-b-session",
            status="standby",
            health_status="healthy",
        )
        db.add(asset)
        db.commit()
        return asset


def _patch_abc_gateway(monkeypatch, harness: ExistingSessionHarness) -> None:
    monkeypatch.setattr(
        f"{ABC_BACKUP_MODULE}.start_standby_authorization_login",
        harness.start_login,
    )
    monkeypatch.setattr(
        f"{ABC_BACKUP_MODULE}.gateway.poll_verification_codes",
        harness.poll_code,
    )
    monkeypatch.setattr(
        f"{ABC_BACKUP_MODULE}.verify_standby_authorization_login",
        harness.verify_login,
    )
    monkeypatch.setattr(
        f"{ABC_BACKUP_MODULE}.managed_two_fa_password",
        lambda *_args: CURRENT_PASSWORD,
    )
    monkeypatch.setattr(f"{ABC_BACKUP_MODULE}.decrypt_session", lambda value: value)
    monkeypatch.setattr(f"{ABC_BACKUP_MODULE}.encrypt_secret", lambda value: f"enc:{value}")
    monkeypatch.setattr(
        f"{ABC_BACKUP_MODULE}.gateway.authorization_identity",
        lambda *_args, **_kwargs: SimpleNamespace(
            telegram_user_id_digest="1" * 64,
            auth_key_fingerprint_digest="3" * 64,
            authorization_hash="987654",
        ),
    )


def test_post_login_abc_reuses_current_a_session_for_code_and_two_fa(
    session: Session,
    monkeypatch,
) -> None:
    account = session.get(TgAccount, 101)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    preview = preview_abc_backup(
        session,
        1,
        account.id,
        idempotency_key="post-login-existing-session",
    )
    harness = ExistingSessionHarness()
    _patch_abc_gateway(monkeypatch, harness)

    result = apply_abc_backup(
        session,
        1,
        account.id,
        idempotency_key="post-login-existing-session",
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="POST-LOGIN-SESSION-REUSE",
    )

    assert result["status"] == "succeeded"
    operation = session.get(TgAuthorizationDrOperation, result["operation_id"])
    assert operation.code_source_authorization_id == primary.id
    assert harness.calls == [
        ("start", account.id, "code"),
        ("poll", account.id, primary.session_ciphertext, primary.developer_app_id),
        ("verify", account.id, CURRENT_PASSWORD),
    ]
    assert account.current_authorization_id == primary.id
    assert account.session_ciphertext == primary.session_ciphertext

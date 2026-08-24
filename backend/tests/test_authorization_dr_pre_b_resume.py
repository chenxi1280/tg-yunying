from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.authorization_dr.abc_backup as abc_backup
import app.services.authorization_dr.abc_backup_resume as abc_backup_resume
import app.services.authorization_dr.online_abc_runner as runner
from app.integrations.telegram.contracts import AuthorizationIdentity
from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationOnlineAbcBatch,
)
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import start_next_online_abc_item
from app.services.authorization_dr.primary_fence import _matches_frozen_primary
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def db_session():
    fixture = abc_tests.session.__wrapped__()
    session = next(fixture)
    try:
        yield session
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def test_partial_stored_identity_accepts_only_matching_expected_values() -> None:
    account = SimpleNamespace(
        current_authorization_id=1,
        authorization_generation=1,
        authorization_fact_generation=1,
        connection_generation=1,
        session_ciphertext="session",
        developer_app_id=2,
    )
    source = SimpleNamespace(
        id=1,
        fact_version=1,
        logical_slot="primary",
        is_current=True,
        provision_region_code="sv",
        session_ciphertext="session",
        developer_app_id=2,
        telegram_user_id_digest="",
        auth_key_fingerprint_digest="a" * 64,
    )
    operation = SimpleNamespace(
        expected_current_authorization_id=1,
        expected_authorization_generation=1,
        expected_authorization_fact_generation=1,
        expected_connection_generation=1,
        expected_code_source_fact_version=1,
        expected_code_source_user_id_digest="u" * 64,
        expected_code_source_auth_key_digest="a" * 64,
    )

    assert _matches_frozen_primary(
        account, source, operation, allow_unpersisted_identity=True
    )
    source.auth_key_fingerprint_digest = "b" * 64
    assert not _matches_frozen_primary(
        account, source, operation, allow_unpersisted_identity=True
    )


def test_resume_accepts_same_approved_pre_b_operation_without_remote_effect(
    db_session, monkeypatch,
) -> None:
    batch_id, item, operation = _stopped_partial_identity_checkpoint(
        db_session, monkeypatch
    )
    account = db_session.get(TgAccount, item.account_id)
    primary = db_session.get(TgAccountAuthorization, item.primary_authorization_id)
    a_before = _a_snapshot(account, primary)

    result = runner.resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-10",
        runtime_release_sha=abc_tests.RELEASE_SHA,
        account_id=item.account_id,
    )

    assert result["batch"]["status"] == "running"
    assert result["current_item"]["account_id"] == item.account_id
    assert result["operations"]["b"]["id"] == operation.id
    assert result["operations"]["b"]["status"] == "approved"
    assert _a_snapshot(account, primary) == a_before


def test_approved_pre_b_resume_rejects_partial_identity_mismatch(
    db_session, monkeypatch,
) -> None:
    batch_id, item, _operation = _stopped_partial_identity_checkpoint(
        db_session, monkeypatch
    )
    primary = db_session.get(TgAccountAuthorization, item.primary_authorization_id)
    primary.auth_key_fingerprint_digest = "f" * 64
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        runner.resume_online_abc_batch(
            db_session,
            batch_id,
            requested_by="requester",
            approved_by="approver",
            approval_ref="ABC-10",
            runtime_release_sha=abc_tests.RELEASE_SHA,
            account_id=item.account_id,
        )

    assert exc_info.value.code == "code_source_changed"
    assert db_session.get(TgAuthorizationOnlineAbcBatch, batch_id).status == "stopped"


def test_same_approved_operation_reenters_b_login_once(db_session, monkeypatch) -> None:
    _batch_id, _item, operation = _stopped_partial_identity_checkpoint(
        db_session, monkeypatch
    )
    executed = []
    monkeypatch.setattr(
        abc_backup_resume,
        "_execute_b_login",
        lambda session, value: executed.append((session, value.id)),
    )

    result = abc_backup_resume.resume_approved_abc_backup(db_session, operation)

    assert executed == [(db_session, operation.id)]
    assert result["operation_id"] == operation.id
    assert result["status"] == "approved"


def _stopped_partial_identity_checkpoint(session, monkeypatch):
    batch_id = abc_tests._apply(session, abc_tests._preview(session)["fingerprint"])[
        "batch_id"
    ]
    command = start_next_online_abc_item(
        session, batch_id, actor="approver", approval_ref="ABC-10"
    )
    item = runner._context(session, batch_id)[1]
    primary = session.get(TgAccountAuthorization, item.primary_authorization_id)
    primary.telegram_user_id_digest = ""
    primary.auth_key_fingerprint_digest = "a" * 64
    session.commit()
    monkeypatch.setattr(
        abc_backup.gateway,
        "authorization_identity",
        lambda *_args, **_kwargs: AuthorizationIdentity(
            "0", "a" * 64, "u" * 64, "f" * 64
        ),
    )
    monkeypatch.setattr(abc_backup, "decrypt_session", lambda value: value)
    monkeypatch.setattr(
        abc_backup,
        "credentials_for_authorization",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    preview = abc_backup.preview_abc_backup(
        session,
        1,
        item.account_id,
        idempotency_key=command["b_idempotency_key"],
        bootstrap_missing_primary_identity=True,
    )
    operation = abc_backup._get_or_create_operation(
        session, preview, "requester", "approver", "ABC-10"
    )
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item.status = "stopped"
    item.outcome = "runner_blocked"
    item.blocker_code = "code_source_changed"
    batch.status = "stopped"
    session.commit()
    return batch_id, item, operation


def _a_snapshot(account, primary):
    return (
        account.current_authorization_id,
        account.session_ciphertext,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.fact_version,
        primary.telegram_user_id_digest,
        primary.auth_key_fingerprint_digest,
    )

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import AccountStatus, TgAccountSecuritySnapshot
from app.services.authorization_dr.contracts import AuthorizationDrError
from tests import test_authorization_dr_sv_two_fa_resume as base


pytestmark = pytest.mark.no_postgres


def _configure_partial(session, primary, *, security, flow, operation) -> None:
    primary.telegram_user_id_digest = ""
    session.delete(security)
    flow.status = AccountStatus.WAITING_2FA.value
    operation.blocker_code = "ValueError"
    session.commit()


def _configure_unknown_snapshot(session, security) -> None:
    security.trusted_session_status = "unknown"
    security.two_fa_status = "unknown"
    security.two_fa_password_ciphertext = ""
    security.two_fa_password_hint = ""
    security.two_fa_password_stored_at = None
    security.external_authorization_count = 0
    security.last_device_scan_at = None
    security.last_2fa_check_at = None
    security.trusted_device_label = ""
    security.last_error = ""
    security.profile_status = "incomplete"
    security.last_hardened_at = base._now()
    session.commit()


def test_healthy_partial_identity_resumes_original_two_fa_flow(monkeypatch) -> None:
    with base._session() as session:
        account, primary, security, flow, operation = base._seed(session)
        _configure_partial(
            session,
            primary,
            security=security,
            flow=flow,
            operation=operation,
        )
        base._patch_common(monkeypatch)
        identities = iter([RuntimeError("session is not authorized"), base._identity()])

        def identity(*_args):
            result = next(identities)
            if isinstance(result, Exception):
                raise result
            return result

        calls = []
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            identity,
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.finish_login",
            lambda code, password, **kwargs: (
                calls.append((code, password, kwargs))
                or (AccountStatus.ACTIVE.value, "raw-authorized")
            ),
        )
        before = base._a_fence(account, primary)
        preview = base._preview(session, operation)
        assert preview["security_snapshot_present"] is False
        assert (preview["primary_user_digest"], preview["primary_authkey_digest"]) == ("", "2" * 64)
        result = base._apply(session, operation, preview["evidence_fingerprint"])
        session.refresh(primary)
        created = session.scalar(
            select(TgAccountSecuritySnapshot).where(
                TgAccountSecuritySnapshot.account_id == account.id,
            )
        )
        assert base._a_fence(account, primary) == before
        assert len(calls) == 1 and calls[0][:2] == (None, "fixed-password")
        assert created.two_fa_password_ciphertext == "managed:fixed-password"
        assert result["candidate_authorization_id"] is not None


def test_partial_identity_accepts_nonasserting_security_snapshot(monkeypatch) -> None:
    with base._session() as session:
        account, primary, security, flow, operation = base._seed(session)
        primary.telegram_user_id_digest = ""
        flow.status = AccountStatus.WAITING_2FA.value
        operation.blocker_code = "ValueError"
        _configure_unknown_snapshot(session, security)
        base._patch_common(monkeypatch)
        identities = iter([RuntimeError("session is not authorized"), base._identity()])

        def identity(*_args):
            result = next(identities)
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            identity,
        )
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.finish_login",
            lambda *_args, **_kwargs: (AccountStatus.ACTIVE.value, "raw-authorized"),
        )
        before = base._a_fence(account, primary)
        preview = base._preview(session, operation)
        assert preview["security_snapshot_present"] is True
        assert preview["security_trusted_session_status"] == "unknown"
        assert preview["security_two_fa_status"] == "unknown"
        result = base._apply(session, operation, preview["evidence_fingerprint"])
        assert base._a_fence(account, primary) == before
        assert result["candidate_authorization_id"] is not None


def test_partial_identity_snapshot_drift_rejects_apply(monkeypatch) -> None:
    with base._session() as session:
        _account, primary, security, flow, operation = base._seed(session)
        primary.telegram_user_id_digest = ""
        flow.status = AccountStatus.WAITING_2FA.value
        operation.blocker_code = "ValueError"
        _configure_unknown_snapshot(session, security)
        preview = base._preview(session, operation)
        security.profile_status = "ready"
        session.commit()
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: pytest.fail("snapshot drift connected to Telegram"),
        )
        with pytest.raises(AuthorizationDrError) as exc_info:
            base._apply(session, operation, preview["evidence_fingerprint"])
        assert exc_info.value.code == "reconcile_evidence_conflict"
        assert operation.reconcile_status == "none"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trusted_session_status", "confirmed"),
        ("two_fa_status", "enabled"),
        ("two_fa_password_ciphertext", "managed-secret"),
        ("two_fa_password_hint", "hint"),
        ("two_fa_password_stored_at", "timestamp"),
        ("external_authorization_count", 1),
        ("last_device_scan_at", "timestamp"),
        ("last_2fa_check_at", "timestamp"),
        ("trusted_device_label", "trusted"),
        ("last_error", "scan-failed"),
    ],
)
def test_partial_identity_rejects_asserting_security_snapshot(field, value) -> None:
    with base._session() as session:
        _account, primary, security, flow, operation = base._seed(session)
        primary.telegram_user_id_digest = ""
        flow.status = AccountStatus.WAITING_2FA.value
        operation.blocker_code = "ValueError"
        _configure_unknown_snapshot(session, security)
        setattr(security, field, base._now() if value == "timestamp" else value)
        session.commit()
        with pytest.raises(AuthorizationDrError):
            base._preview(session, operation)
        assert not session.new and not session.dirty and not session.deleted


@pytest.mark.parametrize("variant", ["uid_present", "auth_key_drift", "snapshot_present"])
def test_healthy_value_error_rejects_unfrozen_identity_shape(monkeypatch, variant) -> None:
    with base._session() as session:
        _account, primary, security, flow, operation = base._seed(session)
        flow.status = AccountStatus.WAITING_2FA.value
        operation.blocker_code = "ValueError"
        if variant != "snapshot_present":
            session.delete(security)
        if variant == "auth_key_drift":
            primary.telegram_user_id_digest = ""
            primary.auth_key_fingerprint_digest = "9" * 64
        session.commit()
        base._patch_common(monkeypatch)
        monkeypatch.setattr(
            "app.services.authorization_dr.sv_two_fa_resume.gateway.authorization_identity",
            lambda *_: pytest.fail("rejected preview connected to Telegram"),
        )
        with pytest.raises(AuthorizationDrError):
            base._preview(session, operation)
        assert not session.new and not session.dirty and not session.deleted

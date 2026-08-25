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

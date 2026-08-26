from __future__ import annotations

import pytest

from app.integrations.telegram.contracts import AccountSecurityOperationResult
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.reconcile import confirm_two_fa_email
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


class _EmailConfirmationGateway:
    def __init__(self, readback_status: str = "enabled") -> None:
        self.readback_status = readback_status
        self.confirmations: list[str] = []

    def confirm_two_fa_email(self, _session, code, _credentials):
        self.confirmations.append(code)
        return AccountSecurityOperationResult(True, "enabled")

    def get_two_fa_status(self, _session, _credentials):
        return AccountSecurityOperationResult(True, self.readback_status)


def test_email_confirmation_requires_enabled_readback(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import reconcile

    gateway = _EmailConfirmationGateway()
    monkeypatch.setattr(reconcile, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "manual-email-confirmation")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.two_fa_status = "manual_required"
        owner.failure_type = "two_fa_email_confirmation_required"
        expected_version = owner.version
        session.commit()

        result = confirm_two_fa_email(
            session,
            1,
            owner.id,
            expected_version=expected_version,
            actor="操作员",
            reason="提交恢复邮箱验证码",
            confirmation_code="123456",
        )

    assert gateway.confirmations == ["123456"]
    assert result.status == "pending"
    assert result.stage == "profile"
    assert result.two_fa_status == "succeeded"


def test_email_confirmation_missing_readback_stays_manual(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import reconcile

    gateway = _EmailConfirmationGateway("missing")
    monkeypatch.setattr(reconcile, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "email-readback-missing")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.two_fa_status = "manual_required"
        owner.failure_type = "two_fa_email_confirmation_required"
        expected_version = owner.version
        session.commit()

        result = confirm_two_fa_email(
            session,
            1,
            owner.id,
            expected_version=expected_version,
            actor="操作员",
            reason="提交恢复邮箱验证码",
            confirmation_code="123456",
        )

    assert result.status == "manual_required"
    assert result.failure_type == "two_fa_remote_confirmed_no_effect"


def test_email_confirmation_local_finish_failure_becomes_unknown(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import reconcile

    gateway = _EmailConfirmationGateway()
    monkeypatch.setattr(reconcile, "gateway", gateway)
    monkeypatch.setattr(
        reconcile,
        "_finish_email_result",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("local_finish_failed")),
    )
    with session_factory() as session:
        _, item = _new_login_item(session, "email-local-finish-unknown")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = owner.stage = "manual_required"
        owner.two_fa_status = "manual_required"
        owner.failure_type = "two_fa_email_confirmation_required"
        expected_version = owner.version
        session.commit()

        result = confirm_two_fa_email(
            session,
            1,
            owner.id,
            expected_version=expected_version,
            actor="操作员",
            reason="提交恢复邮箱验证码",
            confirmation_code="123456",
        )

    assert gateway.confirmations == ["123456"]
    assert result.status == "reconcile_unknown"
    assert result.two_fa_call_state == "unknown"
    assert result.failure_type == "two_fa_remote_unknown"

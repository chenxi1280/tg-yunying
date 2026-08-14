from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram.contracts import LoginChallenge
from app.models import AccountStatus, TelegramDeveloperApp, Tenant, TgAccount, TgLoginFlow
from app.security import decrypt_secret, encrypt_secret
from app.services import accounts as account_service
from app.services._common import _now
from app.services.accounts import LoginFlowFailure, resend_login_code, start_login, verify_login


pytestmark = pytest.mark.no_postgres


class RecordingLoginGateway:
    def __init__(self) -> None:
        self.started_flow_ids: list[int] = []
        self.cancelled_flow_ids: list[int] = []
        self.finish_error: Exception | None = None
        self.finish_result = (AccountStatus.ACTIVE.value, "authorized-session")
        self.finish_calls = 0

    def start_login(self, _method, *, flow_id, **_kwargs) -> LoginChallenge:
        self.started_flow_ids.append(flow_id)
        return LoginChallenge(
            status=AccountStatus.WAITING_CODE.value,
            code_expires_at=_now() + timedelta(minutes=5),
            temporary_session=f"temporary-session:{flow_id}",
            phone_code_hash=f"phone-code-hash:{flow_id}",
        )

    def finish_login(self, *_args, **_kwargs) -> tuple[str, str]:
        self.finish_calls += 1
        if self.finish_error:
            raise self.finish_error
        return self.finish_result

    def cancel_login(self, flow_id: int) -> None:
        self.cancelled_flow_ids.append(flow_id)


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _account_fixture(session: Session) -> TgAccount:
    session.add(Tenant(id=1, name="登录流程测试租户"))
    session.add(
        TelegramDeveloperApp(
            id=1,
            app_name="登录流程测试应用",
            api_id=10001,
            api_hash_ciphertext=encrypt_secret("api-hash"),
            credentials_version=1,
        )
    )
    account = TgAccount(
        id=1,
        tenant_id=1,
        display_name="登录流程测试账号",
        phone_masked="+15550000001",
        phone_ciphertext=encrypt_secret("+15550000001"),
        developer_app_id=1,
        developer_app_version=1,
        status=AccountStatus.PENDING_LOGIN.value,
    )
    session.add(account)
    session.commit()
    return account


def test_start_resumes_same_open_code_challenge(monkeypatch) -> None:
    gateway = RecordingLoginGateway()
    monkeypatch.setattr(account_service, "gateway", gateway)
    with _session() as session:
        account = _account_fixture(session)
        first = start_login(session, account.id, "code")
        resumed = start_login(session, account.id, "code")

        assert resumed["flow_id"] == first["flow_id"]
        assert gateway.started_flow_ids == [first["flow_id"]]
        flow = session.get(TgLoginFlow, first["flow_id"])
        assert decrypt_secret(flow.temporary_session_ciphertext) == f"temporary-session:{flow.id}"
        assert decrypt_secret(flow.phone_code_hash_ciphertext) == f"phone-code-hash:{flow.id}"


def test_resend_supersedes_old_flow_and_invalidates_old_version(monkeypatch) -> None:
    gateway = RecordingLoginGateway()
    monkeypatch.setattr(account_service, "gateway", gateway)
    with _session() as session:
        account = _account_fixture(session)
        first = start_login(session, account.id, "code")
        resent = resend_login_code(session, account.id, first["flow_id"], first["flow_version"])

        old_flow = session.get(TgLoginFlow, first["flow_id"])
        assert resent["flow_id"] != first["flow_id"]
        assert old_flow.status == "superseded"
        assert old_flow.superseded_by_flow_id == resent["flow_id"]
        assert gateway.cancelled_flow_ids == [first["flow_id"]]
        with pytest.raises(LoginFlowFailure) as failure:
            verify_login(session, account.id, first["flow_id"], first["flow_version"], "12345", None)
        assert failure.value.detail["code"] == "login_flow_superseded"
        assert gateway.finish_calls == 0


def test_invalid_remote_code_keeps_exact_flow_retryable(monkeypatch) -> None:
    class PhoneCodeInvalidError(Exception):
        pass

    gateway = RecordingLoginGateway()
    gateway.finish_error = PhoneCodeInvalidError("invalid code")
    monkeypatch.setattr(account_service, "gateway", gateway)
    with _session() as session:
        account = _account_fixture(session)
        started = start_login(session, account.id, "code")
        with pytest.raises(LoginFlowFailure) as failure:
            verify_login(session, account.id, started["flow_id"], started["flow_version"], "00000", None)

        flow = session.get(TgLoginFlow, started["flow_id"])
        assert failure.value.detail["code"] == "login_code_invalid"
        assert flow.status == AccountStatus.WAITING_CODE.value
        assert flow.remote_error_type == "PhoneCodeInvalidError"
        assert decrypt_secret(flow.phone_code_hash_ciphertext) == f"phone-code-hash:{flow.id}"


def test_platform_expiry_rejects_before_remote_verify(monkeypatch) -> None:
    gateway = RecordingLoginGateway()
    monkeypatch.setattr(account_service, "gateway", gateway)
    with _session() as session:
        account = _account_fixture(session)
        started = start_login(session, account.id, "code")
        flow = session.get(TgLoginFlow, started["flow_id"])
        flow.code_expires_at = _now() - timedelta(seconds=1)
        session.commit()

        with pytest.raises(LoginFlowFailure) as failure:
            verify_login(session, account.id, started["flow_id"], started["flow_version"], "12345", None)
        resumed = start_login(session, account.id, "code")
        assert failure.value.detail["code"] == "login_code_expired"
        assert gateway.finish_calls == 0
        assert flow.status == "已过期"
        assert flow.temporary_session_ciphertext is None
        assert flow.phone_code_hash_ciphertext is None
        assert resumed["flow_id"] == started["flow_id"]
        assert gateway.started_flow_ids == [started["flow_id"]]


def test_two_fa_state_updates_flow_without_activating_partial_session(monkeypatch) -> None:
    gateway = RecordingLoginGateway()
    gateway.finish_result = (AccountStatus.WAITING_2FA.value, "two-fa-session")
    monkeypatch.setattr(account_service, "gateway", gateway)
    with _session() as session:
        account = _account_fixture(session)
        started = start_login(session, account.id, "code")
        updated = verify_login(session, account.id, started["flow_id"], started["flow_version"], "12345", None)
        flow = session.get(TgLoginFlow, started["flow_id"])
        flow.code_expires_at = _now() - timedelta(seconds=1)
        session.commit()
        resumed = start_login(session, account.id, "code")

        assert updated.status == AccountStatus.WAITING_2FA.value
        assert updated.session_ciphertext is None
        assert flow.status == AccountStatus.WAITING_2FA.value
        assert decrypt_secret(flow.temporary_session_ciphertext) == "two-fa-session"
        assert resumed["flow_id"] == started["flow_id"]
        assert gateway.started_flow_ids == [started["flow_id"]]

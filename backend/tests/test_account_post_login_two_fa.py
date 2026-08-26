from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select
from telethon import types
from telethon.errors import PasswordHashInvalidError

from app.config import Settings
from app.integrations.telegram import DeveloperAppCredentials, TelethonTelegramGateway
from app.integrations.telegram.contracts import AccountSecurityOperationResult
from app.models import TgAccountFullInitialization, TgAccountSecuritySnapshot
from app.security import decrypt_secret, encrypt_secret
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.contracts import FullInitializationClaim
from app.services.account_post_login_init.two_fa import execute_two_fa_stage
from app.services.account_login.contracts import BatchLoginError
from app.services._common import _now
from app.timezone import BEIJING_TZ, as_beijing
from tests.test_account_post_login_full_init import _new_login_item, session_factory


pytestmark = pytest.mark.no_postgres


class _MissingTwoFaGateway:
    def __init__(self) -> None:
        self.passwords: list[tuple[str, str | None]] = []

    def get_two_fa_status(self, _session, _credentials):
        return AccountSecurityOperationResult(True, "missing")

    def set_two_fa_password(self, _session, password, *, current_password=None, **_kwargs):
        self.passwords.append((password, current_password))
        return AccountSecurityOperationResult(True, "enabled")


class _EnabledTwoFaGateway(_MissingTwoFaGateway):
    def get_two_fa_status(self, _session, _credentials):
        return AccountSecurityOperationResult(True, "enabled")


class _PendingEmailTwoFaGateway(_MissingTwoFaGateway):
    def set_two_fa_password(self, *_args, **_kwargs):
        return AccountSecurityOperationResult(
            True,
            "pending_email_confirmation",
            detail="email confirmation required",
            remote_mutation_started=True,
        )


class _UnknownMutationTwoFaGateway(_MissingTwoFaGateway):
    def set_two_fa_password(self, *_args, **_kwargs):
        return AccountSecurityOperationResult(
            False,
            "failed",
            failure_type="TimeoutError",
            detail="remote result unknown",
            remote_mutation_started=True,
        )


class _UnchangedTwoFaGateway(_EnabledTwoFaGateway):
    def set_two_fa_password(self, *_args, **_kwargs):
        return AccountSecurityOperationResult(
            True,
            "unchanged",
            detail="remote mutation returned false",
            remote_mutation_started=True,
        )


class _FailingCodeClient:
    def __init__(self, code: str) -> None:
        self.code = code

    def fetch_login_materials(self, _url):
        raise BatchLoginError(self.code, "code source unavailable")


class _ResetTwoFaGateway(_MissingTwoFaGateway):
    def __init__(self) -> None:
        super().__init__()
        self.reset_calls = 0
        self.readback_retry_at = _now() + timedelta(days=7)

    def get_two_fa_status(self, _session, _credentials):
        if self.reset_calls == 0:
            return AccountSecurityOperationResult(True, "enabled")
        if self.reset_calls == 1:
            return AccountSecurityOperationResult(
                True,
                "reset_waiting",
                next_retry_at=self.readback_retry_at,
            )
        return AccountSecurityOperationResult(True, "missing")

    def reset_two_fa_password(self, *_args, **_kwargs):
        self.reset_calls += 1
        if self.reset_calls == 1:
            return AccountSecurityOperationResult(
                True,
                "reset_waiting",
                next_retry_at=self.readback_retry_at,
                remote_mutation_started=True,
            )
        return AccountSecurityOperationResult(
            True,
            "reset_completed",
            remote_mutation_started=True,
        )


def test_two_fa_stage_sets_and_records_tenant_fixed_password(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import two_fa

    gateway = _MissingTwoFaGateway()
    monkeypatch.setattr(two_fa, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-stage")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.lease_token = "lease-token"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", "lease-token")

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        snapshot = session.scalar(
            select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == 40)
        )

    assert gateway.passwords == [("fixed-password", None)]
    assert owner.status == "pending"
    assert owner.stage == "profile"
    assert owner.two_fa_status == "succeeded"
    assert snapshot.two_fa_password_source == "platform_fixed_confirmed"
    assert snapshot.fixed_two_fa_version == 1
    assert decrypt_secret(snapshot.two_fa_password_ciphertext) == "fixed-password"


def test_missing_remote_two_fa_is_not_accepted_from_stale_local_proof(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    gateway = _MissingTwoFaGateway()
    monkeypatch.setattr(two_fa, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-stale-local-proof")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        session.add(TgAccountSecuritySnapshot(
            tenant_id=1,
            account_id=40,
            two_fa_status="enabled",
            two_fa_password_ciphertext=encrypt_secret("fixed-password"),
            two_fa_password_source="platform_fixed_confirmed",
            fixed_two_fa_version=owner.fixed_two_fa_version,
            two_fa_authorization_generation=owner.authorization_generation,
            two_fa_evidence_ref="old-fixed-proof",
        ))
        owner.status = "running"
        owner.lease_token = "stale-proof-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", "stale-proof-lease")

    execute_two_fa_stage(session_factory, claim, code_client=object())

    assert gateway.passwords == [("fixed-password", None)]


def test_enabled_remote_with_current_fixed_proof_performs_zero_mutation(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    gateway = _EnabledTwoFaGateway()
    monkeypatch.setattr(two_fa, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-current-fixed-proof")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        session.add(TgAccountSecuritySnapshot(
            tenant_id=1,
            account_id=40,
            two_fa_status="enabled",
            two_fa_password_ciphertext=encrypt_secret("fixed-password"),
            two_fa_password_source="platform_fixed_confirmed",
            fixed_two_fa_version=owner.fixed_two_fa_version,
            two_fa_authorization_generation=owner.authorization_generation,
            two_fa_evidence_ref="current-fixed-proof",
        ))
        owner.status = "running"
        owner.lease_token = "current-fixed-proof-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", owner.lease_token)

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert gateway.passwords == []
    assert owner.two_fa_status == "succeeded"
    assert owner.two_fa_evidence_ref == "current-fixed-proof"


def test_existing_two_fa_without_trusted_source_requires_manual_action(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    gateway = _EnabledTwoFaGateway()
    monkeypatch.setattr(two_fa, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-manual")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.lease_token = "lease-token"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", "lease-token")

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert gateway.passwords == []
    assert owner.status == "manual_required"
    assert owner.failure_type == "two_fa_current_password_unavailable"


def test_expired_code_source_allows_current_password_candidate(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    monkeypatch.setattr(two_fa, "gateway", _EnabledTwoFaGateway())
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-expired-source")
        item.credential_expires_at = _now() + timedelta(minutes=5)
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.lease_token = "expired-source-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", owner.lease_token)

    execute_two_fa_stage(
        session_factory,
        claim,
        code_client=_FailingCodeClient("url_error"),
    )

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert owner.status == "manual_required"
    assert owner.failure_type == "two_fa_current_password_unavailable"


def test_transient_code_source_failure_remains_recheckable(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    monkeypatch.setattr(two_fa, "gateway", _EnabledTwoFaGateway())
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-source-fetch-failed")
        item.credential_expires_at = _now() + timedelta(minutes=5)
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.lease_token = "source-fetch-failed-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", owner.lease_token)

    execute_two_fa_stage(
        session_factory,
        claim,
        code_client=_FailingCodeClient("url_fetch_failed"),
    )

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert owner.status == "failed"
    assert owner.failure_type == "two_fa_source_resolution_failed"


def test_two_fa_reset_waits_then_sets_fixed_password_on_same_owner(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa
    from app.services.account_post_login_init.drain import _claim_next

    gateway = _ResetTwoFaGateway()
    monkeypatch.setattr(two_fa, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-reset")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.source_two_fa_kind = "telegram_reset_requested"
        claim = _running_claim(session, owner, "reset-request-lease")

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        assert owner.status == "pending"
        assert owner.stage == "two_fa"
        assert owner.two_fa_status == "reset_waiting"
        assert owner.next_retry_at == gateway.readback_retry_at
        assert _claim_next(session_factory, reconcile_only=False) is None
        gateway.readback_retry_at = _now() - timedelta(seconds=1)
        claim = _running_claim(session, owner, "reset-finish-lease")

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        assert owner.source_two_fa_kind == "telegram_reset_completed"
        claim = _running_claim(session, owner, "fixed-password-lease")

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert gateway.reset_calls == 2
    assert gateway.passwords == [("fixed-password", None)]
    assert owner.two_fa_status == "succeeded"
    assert owner.stage == "profile"


def _running_claim(session, owner, lease_token: str) -> FullInitializationClaim:
    owner.status = "running"
    owner.stage = "two_fa"
    owner.lease_token = lease_token
    owner.lease_expires_at = _now() + timedelta(seconds=90)
    session.commit()
    return FullInitializationClaim(owner.id, "two_fa", lease_token)


def test_unchanged_two_fa_result_is_not_recorded_as_fixed(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    monkeypatch.setattr(two_fa, "gateway", _UnchangedTwoFaGateway())
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-unchanged-unproven")
        owner = create_or_attach_full_initialization(
            session,
            item,
            actor="操作员",
            source_two_fa_kind="telegram_accepted",
            source_two_fa_password="accepted-source-password",
        )
        owner.status = "running"
        owner.lease_token = "two-fa-unchanged-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", owner.lease_token)

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        snapshot = session.scalar(
            select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == 40)
        )

    assert owner.status == "manual_required"
    assert owner.failure_type == "two_fa_remote_effect_unproven"
    assert snapshot is None or snapshot.two_fa_password_source != "platform_fixed_confirmed"


def test_gateway_classifies_invalid_current_password_before_remote_mutation(monkeypatch) -> None:
    class RejectingClient:
        async def edit_2fa(self, **_kwargs):
            raise PasswordHashInvalidError(request=None)

    gateway = TelethonTelegramGateway(Settings())

    async def authorized_client(*_args, **_kwargs):
        return RejectingClient()

    monkeypatch.setattr(gateway, "_authorized_client", authorized_client)
    result = asyncio.run(
        gateway._set_two_fa_password_async(
            "session",
            "fixed-password",
            DeveloperAppCredentials(
                app_id=1,
                api_id=12345,
                api_hash="hash",
                credentials_version=1,
            ),
            current_password="wrong-password",
        )
    )

    assert result.ok is False
    assert result.failure_type == "two_fa_invalid"
    assert result.remote_mutation_started is False


def test_gateway_maps_telegram_reset_wait_date(monkeypatch) -> None:
    until = (_now() + timedelta(days=7)).replace(tzinfo=BEIJING_TZ)

    class WaitingClient:
        async def __call__(self, _request):
            return types.account.ResetPasswordRequestedWait(until_date=until)

    gateway = TelethonTelegramGateway(Settings())

    async def authorized_client(*_args, **_kwargs):
        return WaitingClient()

    monkeypatch.setattr(gateway, "_authorized_client", authorized_client)
    result = asyncio.run(
        gateway._reset_two_fa_password_async(
            "session",
            DeveloperAppCredentials(
                app_id=1,
                api_id=12345,
                api_hash="hash",
                credentials_version=1,
            ),
        )
    )

    assert result.ok is True
    assert result.status == "reset_waiting"
    assert result.next_retry_at == as_beijing(until)
    assert result.remote_mutation_started is True


@pytest.mark.parametrize(
    "case",
    [
        (
            _PendingEmailTwoFaGateway(),
            "manual_required",
            "confirmed",
            "two_fa_email_confirmation_required",
        ),
        (
            _UnknownMutationTwoFaGateway(),
            "reconcile_unknown",
            "unknown",
            "two_fa_remote_unknown",
        ),
    ],
)
def test_two_fa_non_success_preserves_remote_call_semantics(
    session_factory,
    monkeypatch,
    case,
) -> None:
    from app.services.account_post_login_init import two_fa

    gateway, expected_status, expected_call_state, expected_failure_type = case
    monkeypatch.setattr(two_fa, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, f"two-fa-{expected_status}")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.lease_token = f"{expected_status}-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", owner.lease_token)

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert owner.status == expected_status
    assert owner.two_fa_call_state == expected_call_state
    assert owner.failure_type == expected_failure_type


def test_accepted_login_two_fa_is_used_to_rotate_to_tenant_fixed_password(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    gateway = _EnabledTwoFaGateway()
    monkeypatch.setattr(two_fa, "gateway", gateway)
    with session_factory() as session:
        _, item = _new_login_item(session, "accepted-two-fa")
        owner = create_or_attach_full_initialization(
            session,
            item,
            actor="操作员",
            source_two_fa_kind="telegram_accepted",
            source_two_fa_password="accepted-source-password",
        )
        owner.status = "running"
        owner.lease_token = "accepted-two-fa-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", owner.lease_token)

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)
        snapshot = session.scalar(
            select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == 40)
        )

    assert gateway.passwords == [("fixed-password", "accepted-source-password")]
    assert owner.source_two_fa_password_ciphertext == ""
    assert owner.two_fa_status == "succeeded"
    assert decrypt_secret(snapshot.two_fa_password_ciphertext) == "fixed-password"


def test_two_fa_success_commit_failure_becomes_reconcile_unknown(
    session_factory,
    monkeypatch,
) -> None:
    from app.services.account_post_login_init import two_fa

    gateway = _MissingTwoFaGateway()
    monkeypatch.setattr(two_fa, "gateway", gateway)

    def fail_commit(*_args, **_kwargs):
        raise RuntimeError("commit_failed")

    monkeypatch.setattr(two_fa, "_finish_success", fail_commit)
    with session_factory() as session:
        _, item = _new_login_item(session, "two-fa-commit-unknown")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "running"
        owner.lease_token = "two-fa-commit-unknown-lease"
        session.commit()
        claim = FullInitializationClaim(owner.id, "two_fa", owner.lease_token)

    execute_two_fa_stage(session_factory, claim, code_client=object())

    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, claim.initialization_id)

    assert gateway.passwords == [("fixed-password", None)]
    assert owner.status == "reconcile_unknown"
    assert owner.two_fa_call_state == "unknown"
    assert owner.failure_type == "two_fa_remote_unknown"

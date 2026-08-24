from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    AccountPool,
    AppUser,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountLoginBatchItem,
    TgAccountLoginBatchNotification,
    TgAccountSecuritySnapshot,
)
from app.integrations.telegram.contracts import AccountHealth, LoginChallenge
from app.schemas.account_login import LoginBatchCreateRequest, LoginBatchItemOut, LoginBatchRetryRequest
from app.security import decrypt_session, encrypt_secret
from app.services.account_login.batches import cancel_login_batch, create_login_batch, retry_login_batch_items
from app.services.account_login.contracts import BatchLoginError, LoginMaterials
from app.services.account_login.notifications import finalize_batch_if_terminal, list_platform_notifications
from app.services.account_login.preview import precheck_login_batch


pytestmark = pytest.mark.no_postgres


def _settings():
    return SimpleNamespace(
        account_batch_login_mode="enabled",
        account_batch_login_max_lines=100,
        account_batch_login_item_deadline_seconds=300,
        account_batch_login_code_wait_seconds=120,
        account_batch_login_poll_interval_seconds=3,
        account_batch_login_credential_ttl_seconds=86400,
        account_batch_login_reconcile_seconds=86400,
        account_batch_login_worker_concurrency=4,
        account_batch_login_host_concurrency=1,
        account_batch_login_host_min_interval_seconds=3,
        account_batch_login_developer_app_concurrency=1,
        account_batch_phone_fingerprint_version=1,
        account_batch_phone_fingerprint_versions="1",
        tg_gateway_mode="telethon",
    )


@pytest.fixture()
def session_factory(monkeypatch):
    from app.services.account_login import batches, preview, state

    settings = _settings()
    monkeypatch.setattr(preview, "get_settings", lambda: settings)
    monkeypatch.setattr(preview, "code_source_readiness", lambda _hosts=(): "")
    monkeypatch.setattr(batches, "get_settings", lambda: settings)
    monkeypatch.setattr(state, "get_settings", lambda: settings)
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.add(Tenant(id=1, name="批量登录测试租户"))
        session.add(AccountPool(id=10, tenant_id=1, name="目标分组", pool_purpose="normal"))
        session.add(AppUser(id=20, tenant_id=1, name="测试操作员", role="普通用户", email="batch@example.test"))
        session.add(TelegramDeveloperApp(
            id=30,
            app_name="批量登录测试应用",
            api_id=10030,
            api_hash_ciphertext=encrypt_secret("batch-api-hash"),
            credentials_version=1,
        ))
        session.commit()
    yield factory
    engine.dispose()


def _lines(count: int = 1) -> str:
    rows = []
    for index in range(count):
        uuid_value = f"{index + 1:032x}"
        rows.append(f"+12025550{index + 100:03d}|https://tgbotchecker.com/GetHTML?uuid={uuid_value}")
    return "\n".join(rows)


def _create_payload(
    session: Session,
    lines_text: str,
    *,
    key: str = "batch-key-0001",
    user_id: int = 20,
) -> LoginBatchCreateRequest:
    preview = precheck_login_batch(session, 1, user_id, lines_text, 10)
    return LoginBatchCreateRequest(
        pool_id=10,
        lines_text=lines_text,
        preview_token=preview.preview_token,
        preview_fingerprint=preview.preview_fingerprint,
        idempotency_key=key,
        reason="批量导入测试",
    )


def test_precheck_create_and_idempotent_replay(session_factory) -> None:
    with session_factory() as session:
        payload = _create_payload(session, _lines())
        first = create_login_batch(session, 1, 20, "测试操作员", payload)
        replay = create_login_batch(session, 1, 20, "测试操作员", payload)
        item = session.scalar(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.batch_id == first.id))

    assert first.id == replay.id
    assert item is not None
    assert item.phone_ciphertext != "+12025550100"
    assert "00000000000000000000000000000001" not in item.code_url_ciphertext
    assert item.code_source_uuid_hint == "000000…0001"


def test_builtin_admin_can_create_batch_and_receive_notification(session_factory) -> None:
    from app.auth import admin_user_payload

    admin_id = int(admin_user_payload()["id"])
    with session_factory() as session:
        payload = _create_payload(session, _lines(), key="admin-batch-key-0001", user_id=admin_id)
        batch = create_login_batch(session, 1, admin_id, "系统管理员", payload)
        item = session.scalar(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
        ))
        item.status = "failed"
        item.failure_type = "code_timeout"
        assert finalize_batch_if_terminal(session, batch.id)
        session.commit()
        notifications = list(session.scalars(select(TgAccountLoginBatchNotification).where(
            TgAccountLoginBatchNotification.batch_id == batch.id,
        )))

    assert batch.recipient_user_id == admin_id
    assert {row.channel for row in notifications} == {"platform", "tg_bot"}
    assert {row.recipient_user_id for row in notifications} == {admin_id}


def test_idempotency_key_rejects_changed_request(session_factory) -> None:
    with session_factory() as session:
        payload = _create_payload(session, _lines())
        create_login_batch(session, 1, 20, "测试操作员", payload)
        changed = payload.model_copy(update={"reason": "不同原因"})
        with pytest.raises(BatchLoginError) as error:
            create_login_batch(session, 1, 20, "测试操作员", changed)

    assert error.value.code == "idempotency_conflict"


def test_terminal_batch_notification_separates_failure_unknown_and_warning(session_factory) -> None:
    with session_factory() as session:
        payload = _create_payload(session, _lines(3), key="batch-key-0002")
        batch = create_login_batch(session, 1, 20, "测试操作员", payload)
        items = list(session.scalars(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
        ).order_by(TgAccountLoginBatchItem.line_no)))
        items[0].status = "failed"
        items[0].failure_type = "code_timeout"
        items[1].status = "unresolved"
        items[1].failure_type = "login_remote_unknown"
        items[2].status = "succeeded_with_warning"
        items[2].warning_detail = "online readback failed"
        assert finalize_batch_if_terminal(session, batch.id)
        session.commit()
        notifications = list(session.scalars(select(TgAccountLoginBatchNotification).where(
            TgAccountLoginBatchNotification.batch_id == batch.id,
        ).order_by(TgAccountLoginBatchNotification.channel)))

    assert batch.status == "completed_with_unresolved"
    assert (batch.success_count, batch.failed_count, batch.unresolved_count, batch.warning_count) == (1, 1, 1, 1)
    assert {row.channel for row in notifications} == {"platform", "tg_bot"}
    summary = json.loads(notifications[0].summary_json)
    assert summary["failed"][0]["reason"] == "code_timeout"
    assert summary["unresolved"][0]["reason"] == "login_remote_unknown"
    assert summary["warning"][0]["phone_masked"].endswith("0102")
    assert "00000000000000000000000000000003" not in notifications[0].summary_json


def test_platform_notifications_show_latest_initial_after_retry(session_factory) -> None:
    with session_factory() as session:
        batch = create_login_batch(session, 1, 20, "测试操作员", _create_payload(session, _lines(), key="batch-key-notify-latest"))
        item = session.scalar(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
        ))
        item.status = "failed"
        item.failure_type = "item_deadline_exceeded"
        assert finalize_batch_if_terminal(session, batch.id)
        batch.execution_generation = 2
        item.status = "succeeded"
        item.failure_type = ""
        assert finalize_batch_if_terminal(session, batch.id)
        session.commit()
        visible = list_platform_notifications(session, 1, 20, unacknowledged=True)
        stored = list(session.scalars(select(TgAccountLoginBatchNotification).where(
            TgAccountLoginBatchNotification.batch_id == batch.id,
            TgAccountLoginBatchNotification.channel == "platform",
        )))

    assert len(stored) == 2
    assert len(visible) == 1
    assert visible[0]["execution_generation"] == 2
    assert visible[0]["summary"]["counts"]["success"] == 1


def test_expired_bot_delivery_claim_is_recovered_after_worker_crash(session_factory) -> None:
    from datetime import timedelta

    from app.services._common import _now
    from app.services.account_login.notifications import _claim_bot_delivery

    with session_factory() as session:
        batch = create_login_batch(
            session, 1, 20, "测试操作员",
            _create_payload(session, _lines(), key="batch-key-outbox-recovery"),
        )
        item = session.scalar(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.batch_id == batch.id))
        item.status = "failed"
        item.failure_type = "code_timeout"
        assert finalize_batch_if_terminal(session, batch.id)
        tenant = session.get(Tenant, 1)
        tenant.telegram_bot_token_ciphertext = encrypt_secret("bot-token")
        tenant.admin_chat_id = "1001"
        notification = session.scalar(select(TgAccountLoginBatchNotification).where(
            TgAccountLoginBatchNotification.batch_id == batch.id,
            TgAccountLoginBatchNotification.channel == "tg_bot",
        ))
        notification.delivery_status = "sending"
        notification.delivery_attempts = 1
        notification.next_retry_at = _now() - timedelta(seconds=1)
        session.commit()

    delivery = _claim_bot_delivery(session_factory)
    with session_factory() as session:
        recovered = session.get(TgAccountLoginBatchNotification, notification.id)

    assert delivery is not None and delivery.notification_id == notification.id
    assert recovered.delivery_status == "sending"
    assert recovered.delivery_attempts == 2
    assert recovered.next_retry_at > _now()


def test_item_deadline_failure_does_not_block_next_line(session_factory) -> None:
    from datetime import timedelta

    from app.services._common import _now
    from app.services.account_login.remote_phases import _wait_or_timeout
    from app.services.account_login.state import claim_batch_phase
    from app.models import TgAccountLoginBatchAttempt

    with session_factory() as session:
        batch = create_login_batch(session, 1, 20, "测试操作员", _create_payload(session, _lines(2), key="batch-key-timeout"))
        first = session.scalar(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
        ).order_by(TgAccountLoginBatchItem.line_no))
        attempt = session.get(TgAccountLoginBatchAttempt, first.current_attempt_id)
        first.status = "waiting"
        first.phase = "wait_code"
        attempt.phase = "wait_code"
        attempt.deadline_at = _now() - timedelta(seconds=1)
        attempt.code_wait_until_at = _now() + timedelta(seconds=30)
        session.commit()

    with session_factory() as session:
        claim = claim_batch_phase(session, batch.id)
    assert claim is not None and claim.item_id == first.id
    _wait_or_timeout(session_factory, claim)

    with session_factory() as session:
        failed = session.get(TgAccountLoginBatchItem, first.id)
        next_claim = claim_batch_phase(session, batch.id)
    assert failed.status == "failed"
    assert failed.failure_type == "item_deadline_exceeded"
    assert next_claim is not None and next_claim.item_id != first.id


def test_code_wait_timeout_is_reported_before_equal_item_deadline(session_factory) -> None:
    from datetime import timedelta

    from app.services._common import _now
    from app.services.account_login.remote_phases import _wait_or_timeout
    from app.services.account_login.state import claim_batch_phase
    from app.models import TgAccountLoginBatchAttempt

    with session_factory() as session:
        batch = create_login_batch(session, 1, 20, "测试操作员", _create_payload(session, _lines(1), key="batch-key-code-timeout"))
        item = session.scalar(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
        ))
        attempt = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
        due_at = _now() - timedelta(seconds=1)
        item.status = "waiting"
        item.phase = "wait_code"
        attempt.phase = "wait_code"
        attempt.deadline_at = due_at
        attempt.code_wait_until_at = due_at
        session.commit()

    with session_factory() as session:
        claim = claim_batch_phase(session, batch.id)
    assert claim is not None and claim.item_id == item.id
    _wait_or_timeout(session_factory, claim)

    with session_factory() as session:
        failed = session.get(TgAccountLoginBatchItem, item.id)
    assert failed.status == "failed"
    assert failed.failure_type == "code_timeout"


def test_cancel_skips_unstarted_lines_and_clears_credentials(session_factory) -> None:
    with session_factory() as session:
        batch = create_login_batch(
            session, 1, 20, "测试操作员",
            _create_payload(session, _lines(2), key="batch-key-cancel"),
        )
        cancelled = cancel_login_batch(
            session, 1, batch.id, batch.state_version, "测试操作员", "停止本批次",
        )
        items = list(session.scalars(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
        ).order_by(TgAccountLoginBatchItem.line_no)))

    assert cancelled.status == "cancelled"
    assert all(item.status == "skipped" for item in items)
    assert all(item.failure_type == "manual_interrupted" for item in items)
    assert all(item.code_url_ciphertext is None for item in items)


def test_stale_started_call_becomes_unresolved_and_next_line_advances(session_factory) -> None:
    from datetime import timedelta

    from app.models import TgAccountLoginBatchAttempt
    from app.services._common import _now
    from app.services.account_login.reconciliation import drain_account_login_reconciliation
    from app.services.account_login.state import claim_batch_phase

    with session_factory() as session:
        batch = create_login_batch(session, 1, 20, "测试操作员", _create_payload(session, _lines(2), key="batch-key-unknown"))
        first = session.scalar(select(TgAccountLoginBatchItem).where(
            TgAccountLoginBatchItem.batch_id == batch.id,
        ).order_by(TgAccountLoginBatchItem.line_no))
        attempt = session.get(TgAccountLoginBatchAttempt, first.current_attempt_id)
        first.status = "running"
        first.phase = "send_code"
        attempt.phase = "send_code"
        attempt.deadline_at = _now() - timedelta(seconds=1)
        attempt.send_call_state = "started"
        session.commit()

    with session_factory() as session:
        assert claim_batch_phase(session, batch.id) is None
    drain_account_login_reconciliation(session_factory, 1)

    with session_factory() as session:
        unresolved = session.get(TgAccountLoginBatchItem, first.id)
        next_claim = claim_batch_phase(session, batch.id)
    assert unresolved.status == "unresolved"
    assert unresolved.failure_type == "login_remote_unknown"
    assert next_claim is not None and next_claim.item_id != first.id


def test_unresolved_retry_requires_completed_probe_and_supersedes_old_attempt(session_factory) -> None:
    from app.models import TgAccountLoginBatchAttempt
    from app.services._common import _now

    with session_factory() as session:
        batch = create_login_batch(
            session, 1, 20, "测试操作员",
            _create_payload(session, _lines(), key="batch-key-unresolved-retry"),
        )
        item = session.scalar(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.batch_id == batch.id))
        attempt = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
        item.status = "unresolved"
        item.phase = "unresolved"
        attempt.reconcile_status = "pending"
        attempt.last_reconciled_at = _now()
        finalize_batch_if_terminal(session, batch.id)
        session.commit()
        payload = LoginBatchRetryRequest(
            item_ids=[item.id],
            expected_state_version=batch.state_version,
            expected_attempt_id=attempt.id,
            expected_attempt_version=attempt.state_version,
            expected_resolution_version=batch.resolution_version,
            confirm_remote_unknown=True,
            reason="已确认重复调用风险",
        )
        retry_login_batch_items(session, 1, batch.id, payload, "测试操作员")
        old_attempt = session.get(TgAccountLoginBatchAttempt, attempt.id)
        session.refresh(item)

    assert item.status == "pending"
    assert item.execution_generation == 2
    assert item.current_attempt_id != old_attempt.id
    assert old_attempt.reconcile_status == "superseded"


class _LoginCodeClient:
    def __init__(self) -> None:
        self.calls = 0

    def fetch_login_materials(self, _url: str) -> LoginMaterials:
        self.calls += 1
        if self.calls == 1:
            return LoginMaterials("11111", "", "old-time", "old-fetch")
        return LoginMaterials("22222", "two-fa-secret", "new-time", "new-fetch")


class _SuccessfulTwoFaGateway:
    def __init__(self) -> None:
        self.finish_calls: list[tuple[str | None, str | None]] = []

    def start_login(self, _method, **_kwargs) -> LoginChallenge:
        return LoginChallenge(
            status="等待验证码",
            temporary_session="temporary-session",
            phone_code_hash="phone-code-hash",
        )

    def finish_login(self, code, password_2fa, **_kwargs):
        self.finish_calls.append((code, password_2fa))
        if code:
            return "等待2FA", "temporary-after-code"
        return "在线", "authorized-session"

    def check_account_health_isolated(self, _session_ciphertext, _credentials) -> AccountHealth:
        return AccountHealth(status="在线", health_score=95, detail="authorized")


def test_full_new_account_two_fa_flow_persists_binding_without_password(session_factory, monkeypatch) -> None:
    from app.services import account_phone_aliases
    from app.services.account_login import drain, local_phases, remote_phases

    with session_factory() as session:
        batch = create_login_batch(
            session,
            1,
            20,
            "测试操作员",
            _create_payload(session, _lines(), key="batch-key-full-flow"),
        )
    settings = _settings()
    settings.account_batch_login_host_min_interval_seconds = 0
    for module in (account_phone_aliases, drain, local_phases, remote_phases):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    login_gateway = _SuccessfulTwoFaGateway()
    monkeypatch.setattr(remote_phases, "gateway", login_gateway)
    code_client = _LoginCodeClient()

    for _ in range(12):
        drain.drain_account_login_batches(session_factory, 1, code_client=code_client)

    with session_factory() as session:
        item = session.scalar(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.batch_id == batch.id))
        account = session.get(TgAccount, item.account_id)
        security = session.scalar(select(TgAccountSecuritySnapshot).where(TgAccountSecuritySnapshot.account_id == account.id))
        notification = session.scalar(select(TgAccountLoginBatchNotification).where(
            TgAccountLoginBatchNotification.batch_id == batch.id,
            TgAccountLoginBatchNotification.channel == "platform",
        ))
        safe_item = LoginBatchItemOut.model_validate(item).model_dump()

    assert item.status == "succeeded"
    assert account.status == "在线"
    assert account.pool_id == 10
    assert account.code_source_note == "tgbotchecker · 000000…0001"
    assert decrypt_session(account.session_ciphertext) == "authorized-session"
    assert login_gateway.finish_calls == [("22222", None), (None, "two-fa-secret")]
    assert security is None or not security.two_fa_password_ciphertext
    assert notification is not None
    assert "code_url_ciphertext" not in safe_item
    assert "phone_ciphertext" not in safe_item
    assert "code_source_uuid_fingerprint" not in safe_item


def _create_unresolved_reconcile_fixture(session_factory) -> int:
    from datetime import timedelta

    from app.models import TgAccountLoginBatchAttempt, TgLoginFlow
    from app.services._common import _now

    with session_factory() as session:
        batch = create_login_batch(
            session, 1, 20, "测试操作员",
            _create_payload(session, _lines(), key="batch-key-reconcile"),
        )
        item = session.scalar(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.batch_id == batch.id))
        attempt = session.get(TgAccountLoginBatchAttempt, item.current_attempt_id)
        account = TgAccount(
            tenant_id=1, pool_id=10, display_name="待对账账号", phone_masked=item.phone_masked,
            phone_ciphertext=encrypt_secret("+12025550100"), developer_app_id=30, developer_app_version=1,
            status="等待2FA",
        )
        session.add(account)
        session.flush()
        flow = TgLoginFlow(
            tenant_id=1, account_id=account.id, method="code", status="等待2FA",
            authorization_role="primary", developer_app_id=30,
            batch_login_attempt_id=attempt.id, batch_login_generation=1,
            temporary_session_ciphertext=encrypt_secret("late-authorized-session"),
        )
        session.add(flow)
        session.flush()
        item.account_id = account.id
        item.status = "unresolved"
        item.phase = "unresolved"
        item.failure_type = "login_remote_unknown"
        attempt.flow_id = flow.id
        attempt.flow_version = flow.flow_version
        attempt.reconcile_status = "pending"
        attempt.reconcile_until_at = _now() + timedelta(hours=1)
        finalize_batch_if_terminal(session, batch.id)
        session.commit()
        return batch.id


class _ReconcileGateway:
    def check_account_health_isolated(self, session_ciphertext, _credentials):
        assert decrypt_session(session_ciphertext) == "late-authorized-session"
        return AccountHealth(status="在线", health_score=95, detail="authorized")


def test_unresolved_reconciliation_uses_temporary_session_and_emits_correction(session_factory, monkeypatch) -> None:
    from app.models import TgAccountLoginBatch
    from app.services.account_login import reconciliation

    batch_id = _create_unresolved_reconcile_fixture(session_factory)
    monkeypatch.setattr(reconciliation, "gateway", _ReconcileGateway())
    monkeypatch.setattr(reconciliation, "get_settings", _settings)
    assert reconciliation.drain_account_login_reconciliation(session_factory, 2) == 1

    with session_factory() as session:
        item = session.scalar(select(TgAccountLoginBatchItem).where(TgAccountLoginBatchItem.batch_id == batch_id))
        account = session.get(TgAccount, item.account_id)
        corrected_batch = session.get(TgAccountLoginBatch, batch_id)
        correction = session.scalar(select(TgAccountLoginBatchNotification).where(
            TgAccountLoginBatchNotification.batch_id == batch_id,
            TgAccountLoginBatchNotification.channel == "platform",
            TgAccountLoginBatchNotification.resolution_version == 1,
        ))

    assert item.status == "succeeded"
    assert decrypt_session(account.session_ciphertext) == "late-authorized-session"
    assert corrected_batch.resolution_version == 1
    assert json.loads(correction.summary_json)["corrections"][0]["line_no"] == 1

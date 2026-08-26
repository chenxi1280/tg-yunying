from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import (
    AccountPool,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountFullInitialization,
    TgAccountLoginBatch,
    TgAccountLoginBatchAttempt,
    TgAccountLoginBatchItem,
    TgAccountLoginPostInitializationBinding,
    TgAccountSecurityBatch,
)
from app.security import decrypt_secret, encrypt_secret, encrypt_session
from app.services.account_login.batches import skip_cancellable_items
from app.services.account_post_login_init.binding import create_or_attach_full_initialization
from app.services.account_post_login_init.parent import sync_parent_bindings
from app.services.account_post_login_init.read import post_login_initialization_detail


pytestmark = pytest.mark.no_postgres
FULL_INIT_POLICY = "normal_full_init_v1"


@pytest.fixture()
def session_factory(monkeypatch):
    from app.services.account_post_login_init import binding

    monkeypatch.setattr(
        binding,
        "get_settings",
        lambda: SimpleNamespace(account_post_login_init_secret_ttl_seconds=900),
    )
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as session:
        session.add(Tenant(
            id=1,
            name="完整初始化测试租户",
            fixed_two_fa_password_ciphertext=encrypt_secret("fixed-password"),
            fixed_two_fa_password_version=1,
        ))
        session.add(AccountPool(id=10, tenant_id=1, name="普通池", pool_purpose="normal"))
        session.add(TelegramDeveloperApp(
            id=30,
            app_name="测试应用",
            api_id=10030,
            api_hash_ciphertext=encrypt_secret("api-hash"),
        ))
        session.add(TgAccount(
            id=40,
            tenant_id=1,
            pool_id=10,
            display_name="待初始化账号",
            phone_masked="+120****0100",
            phone_ciphertext=encrypt_secret("+12025550100"),
            developer_app_id=30,
            session_ciphertext=encrypt_session("authorized-session"),
            status="在线",
        ))
        session.commit()
    yield factory
    engine.dispose()


def _new_login_item(session, key: str, line_no: int = 1):
    batch = TgAccountLoginBatch(
        tenant_id=1,
        pool_id=10,
        created_by="操作员",
        recipient_user_id=20,
        idempotency_key=key,
        request_fingerprint=key.ljust(64, "0")[:64],
        total_count=1,
        reason="完整初始化测试",
        trace_id=f"trace-{key}",
        initialization_policy=FULL_INIT_POLICY,
    )
    session.add(batch)
    session.flush()
    item = TgAccountLoginBatchItem(
        batch_id=batch.id,
        tenant_id=1,
        line_no=line_no,
        phone_masked="+120****0100",
        phone_fingerprint=f"phone-{key}".ljust(64, "0")[:64],
        phone_fingerprint_version=1,
        phone_ciphertext=encrypt_secret("+12025550100"),
        code_url_ciphertext=encrypt_secret("https://example.test/code"),
        code_source_host="example.test",
        code_source_uuid_fingerprint=f"uuid-{key}".ljust(64, "0")[:64],
        code_source_uuid_hint="uuid",
        route_hint="existing_probe_required",
        route="already_authorized",
        account_id=40,
        initialization_policy=FULL_INIT_POLICY,
    )
    session.add(item)
    session.flush()
    return batch, item


def test_reentered_batch_reuses_same_account_initialization(session_factory) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, "first-batch")
        first = create_or_attach_full_initialization(
            session, first_item, actor="操作员",
            source_two_fa_kind="telegram_accepted",
            source_two_fa_password="source-password",
        )
        _, second_item = _new_login_item(session, "second-batch")
        second = create_or_attach_full_initialization(
            session, second_item, actor="操作员", source_two_fa_kind="telegram_missing",
        )
        session.commit()
        bindings = list(session.scalars(select(TgAccountLoginPostInitializationBinding)))

    assert first.id == second.id
    assert first.status == "waiting_login_parent"
    assert len(bindings) == 2
    assert decrypt_secret(first.source_two_fa_password_ciphertext) == "source-password"
    assert first.source_two_fa_kind == "telegram_accepted"


def test_post_initialization_detail_is_secret_free(session_factory) -> None:
    with session_factory() as session:
        batch, item = _new_login_item(session, "post-init-detail")
        owner = create_or_attach_full_initialization(
            session, item, actor="操作员", source_two_fa_kind="telegram_accepted",
            source_two_fa_password="source-password",
        )
        session.commit()
        detail = post_login_initialization_detail(
            session, 1, batch_id=batch.id, item_id=item.id,
        )

    assert detail["id"] == owner.id
    assert detail["source_two_fa_kind"] == "telegram_accepted"
    assert detail["two_fa_evidence_present"] is False
    assert detail["abc_request_status"] == "not_created"
    assert all("ciphertext" not in key and "password" not in key for key in detail)


def test_cancelling_parent_keeps_credentials_for_mandatory_initialization(
    session_factory,
) -> None:
    with session_factory() as session:
        batch, item = _new_login_item(session, "cancel-parent")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        item.status = "post_initialization_waiting"
        item.phase = "post_initialization_waiting"
        owner.status = "pending"
        skip_cancellable_items(session, batch.id)
        session.commit()
        encrypted_url = item.code_url_ciphertext
        owner.status = "succeeded"
        sync_parent_bindings(session, owner)
        session.commit()
        session.refresh(batch)

    assert item.status == "skipped"
    assert item.post_initialization_status == "succeeded"
    assert batch.fully_initialized_count == 1
    assert encrypted_url


def test_off_mode_still_purges_expired_source_password(session_factory, monkeypatch) -> None:
    from app.services._common import _now
    from app.services.account_post_login_init import drain

    monkeypatch.setattr(
        drain,
        "get_settings",
        lambda: SimpleNamespace(
            account_post_login_init_mode="off",
            account_post_login_init_worker_concurrency=2,
        ),
    )
    with session_factory() as session:
        _, item = _new_login_item(session, "expired-secret")
        owner = create_or_attach_full_initialization(
            session, item, actor="操作员", source_two_fa_kind="telegram_accepted",
            source_two_fa_password="expired-password",
        )
        owner.source_secret_expires_at = _now() - timedelta(seconds=1)
        session.commit()

    assert drain.drain_account_post_login_initializations(session_factory, 1) == 0
    with session_factory() as session:
        owner = session.get(TgAccountFullInitialization, owner.id)
    assert owner.source_two_fa_password_ciphertext == ""
    assert owner.source_secret_expires_at is None


def test_reconcile_only_mode_does_not_start_pending_two_fa(session_factory, monkeypatch) -> None:
    from app.services.account_post_login_init import drain

    calls: list[int] = []
    monkeypatch.setattr(
        drain,
        "get_settings",
        lambda: SimpleNamespace(
            account_post_login_init_mode="reconcile_only",
            account_post_login_init_worker_concurrency=2,
        ),
    )
    monkeypatch.setattr(drain, "execute_two_fa_stage", lambda *_args, **_kwargs: calls.append(1))
    with session_factory() as session:
        _, item = _new_login_item(session, "reconcile-only")
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "pending"
        session.commit()

    assert drain.drain_account_post_login_initializations(session_factory, 1) == 0
    assert calls == []


def test_new_authorization_generation_replaces_drifted_owner(session_factory) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, "generation-one")
        first = create_or_attach_full_initialization(session, first_item, actor="操作员")
        first_id = first.id
        account = session.get(TgAccount, 40)
        account.session_ciphertext = encrypt_session("new-authorized-session")
        _, second_item = _new_login_item(session, "generation-two")
        second = create_or_attach_full_initialization(session, second_item, actor="操作员")
        session.commit()
        first = session.get(TgAccountFullInitialization, first_id)

    assert second.id != first.id
    assert second.generation == 2
    assert first.status == "failed"
    assert first.failure_type == "post_init_policy_drift"


def test_new_batch_reuses_unknown_owner_without_replaying_mutation(session_factory) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, "unknown-first")
        first = create_or_attach_full_initialization(session, first_item, actor="操作员")
        first.status = "reconcile_unknown"
        first.stage = "reconcile_unknown"
        repeated = create_or_attach_full_initialization(session, first_item, actor="操作员")
        _, second_item = _new_login_item(session, "unknown-second")
        second = create_or_attach_full_initialization(session, second_item, actor="操作员")
        session.commit()

    assert repeated.id == first.id
    assert second.id == first.id
    assert second.status == "pending"
    assert second.stage == "reconcile"


def test_new_batch_retries_only_safe_profile_readback_unknown(session_factory) -> None:
    with session_factory() as session:
        _, first_item = _new_login_item(session, "profile-unknown-first")
        first = create_or_attach_full_initialization(session, first_item, actor="操作员")
        profile_batch = TgAccountSecurityBatch(tenant_id=1, status="completed")
        session.add(profile_batch)
        session.flush()
        first.status = "reconcile_unknown"
        first.stage = "reconcile_unknown"
        first.profile_status = "reconcile_unknown"
        first.profile_batch_id = profile_batch.id
        first.failure_type = "profile_readback_unknown"
        _, second_item = _new_login_item(session, "profile-unknown-second")
        second = create_or_attach_full_initialization(session, second_item, actor="操作员")
        session.commit()

    assert second.id == first.id
    assert second.status == "pending"
    assert second.stage == "reconcile"
    assert second.profile_status == "reconcile_unknown"


def test_parent_item_only_succeeds_after_full_initialization(session_factory) -> None:
    with session_factory() as session:
        batch, item = _new_login_item(session, "parent-terminal")
        attempt = TgAccountLoginBatchAttempt(
            item_id=item.id, batch_id=batch.id, tenant_id=1,
            execution_generation=1, phase="post_initialization_waiting",
        )
        session.add(attempt)
        session.flush()
        item.current_attempt_id = attempt.id
        item.status = "post_initialization_waiting"
        item.authorization_status = "confirmed"
        owner = create_or_attach_full_initialization(session, item, actor="操作员")
        owner.status = "succeeded"
        owner.stage = "completed"
        owner.two_fa_status = "succeeded"
        owner.profile_status = "succeeded"
        owner.abc_status = "succeeded"
        sync_parent_bindings(session, owner)
        session.commit()
        session.refresh(batch)

    assert item.status == "succeeded"
    assert batch.status == "completed"
    assert batch.authorized_count == 1
    assert batch.fully_initialized_count == 1

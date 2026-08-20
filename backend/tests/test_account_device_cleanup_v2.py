from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
    TgAccountSecurityBatchItem,
)
from app.security import encrypt_secret
from app.services._common import _now
from app.services.account_device_cleanup_v2 import create_device_cleanup_batch
from app.timezone import as_beijing_aware


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        _seed_accounts(db)
        yield db


def _seed_accounts(session: Session) -> None:
    session.add(Tenant(id=1, name="cleanup test tenant"))
    session.add(TelegramDeveloperApp(
        id=1,
        app_name="App A",
        api_id=1001,
        api_hash_ciphertext=encrypt_secret("test-hash"),
        credentials_version=1,
    ))
    session.flush()
    _seed_account(session, 101, login_age=timedelta(hours=49))
    _seed_account(session, 102, login_age=timedelta(hours=47))
    _seed_account(session, 103, login_age=None)
    session.commit()


def _seed_account(session: Session, account_id: int, *, login_age: timedelta | None) -> None:
    session.add(TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        session_ciphertext=f"primary-{account_id}",
        developer_app_id=1,
        developer_app_version=1,
    ))
    session.flush()
    session.add(TgAccountAuthorization(
        tenant_id=1,
        account_id=account_id,
        role="primary",
        logical_slot="primary",
        is_slot_current=True,
        is_current=True,
        provision_region_code="sv",
        developer_app_id=1,
        developer_app_api_id_snapshot=1001,
        session_ciphertext=f"primary-{account_id}",
        telegram_login_at=as_beijing_aware(_now()) - login_age if login_age else None,
        telegram_authorization_hash_ciphertext=encrypt_secret(f"hash-{account_id}"),
        remote_authorization_state="active",
        protected_from_cleanup=True,
        fact_version=4,
    ))


def test_batch_skips_accounts_without_48_hour_login_age(session: Session) -> None:
    batch = create_device_cleanup_batch(
        session,
        1,
        [103, 101, 102],
        actor="operator",
        reason="cleanup test",
        idempotency_key="cleanup-1",
    )

    items = list(session.scalars(select(TgAccountSecurityBatchItem).where(
        TgAccountSecurityBatchItem.batch_id == batch.id,
    ).order_by(TgAccountSecurityBatchItem.account_id)))
    assert batch.requested_count == 3
    assert batch.eligible_count == 1
    assert batch.skipped_count == 2
    assert [item.status for item in items] == ["pending", "skipped", "skipped"]
    assert [item.skipped_reason for item in items] == ["", "login_age_not_over_48h", "login_time_missing"]
    assert items[0].executor_fact_version == 4


def test_idempotency_key_requires_same_exact_account_scope(session: Session) -> None:
    first = create_device_cleanup_batch(
        session,
        1,
        [101, 102],
        actor="operator",
        reason="cleanup test",
        idempotency_key="cleanup-2",
    )
    same = create_device_cleanup_batch(
        session,
        1,
        [102, 101],
        actor="operator",
        reason="cleanup test",
        idempotency_key="cleanup-2",
    )
    assert same.id == first.id

    with pytest.raises(ValueError, match="security_batch_target_changed"):
        create_device_cleanup_batch(
            session,
            1,
            [101],
            actor="operator",
            reason="cleanup test",
            idempotency_key="cleanup-2",
        )

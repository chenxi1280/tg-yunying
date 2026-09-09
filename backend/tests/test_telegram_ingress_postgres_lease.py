"""Read actual timestamptz values before validating and persisting ingress."""
from datetime import datetime, timedelta, timezone
import os

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models import Tenant, TgAccount, TgAccountAuthorization
from app.models.telegram_updates import TelegramAuthorizationUpdateEvent, TelegramAuthorizationUpdateState
from app.services.task_center import group_clone_precheck, telegram_update_ingress
from app.timezone import BEIJING_TZ, as_beijing_aware
import test_group_clone_update_ingress as ingress_tests

NOW = datetime(2026, 9, 9, 17, 0)


@pytest.mark.parametrize("seconds", [-1, 0, 60])
@pytest.mark.parametrize("zone", [timezone.utc, BEIJING_TZ])
def test_postgres_lease_and_ingress(monkeypatch, seconds, zone):
    monkeypatch.setattr(telegram_update_ingress, "_now", lambda: NOW)
    monkeypatch.setattr(group_clone_precheck, "_now", lambda: NOW)
    engine = create_engine(os.environ["TEST_DATABASE_URL"])
    try:
        with Session(engine) as session:
            state = _new_state(session, seconds=seconds, zone=zone)
            state_id = state.id
            session.expire_all()
            state = session.get(TelegramAuthorizationUpdateState, state_id)
            assert state.lease_expires_at.tzinfo is not None
            assert group_clone_precheck._update_ingress_ready(state) is (seconds > 0)
            if seconds > 0:
                event, _ = ingress_tests._write_ingress(
                    session, state, ingress_tests._ingress("pg-lease", message_id=11, pts=101),
                )
                assert event.id is not None
            else:
                with pytest.raises(ValueError, match="telegram_update_collector_lease_expired"):
                    ingress_tests._write_ingress(
                        session, state, ingress_tests._ingress("pg-lease", message_id=11, pts=101),
                    )
            count = session.scalar(select(func.count()).select_from(TelegramAuthorizationUpdateEvent).where(
                TelegramAuthorizationUpdateEvent.authorization_update_state_id == state.id,
            ))
            assert count == int(seconds > 0)
            session.rollback()
    finally:
        engine.dispose()


def _new_state(session, *, seconds, zone):
    tenant = Tenant(name="clone-lease-pg")
    session.add(tenant)
    session.flush()
    account = TgAccount(tenant_id=tenant.id, display_name="lease-test", phone_masked="test")
    session.add(account)
    session.flush()
    authorization = TgAccountAuthorization(tenant_id=tenant.id, account_id=account.id, is_current=True)
    session.add(authorization)
    session.flush()
    state = TelegramAuthorizationUpdateState(
        tenant_id=tenant.id, account_id=account.id, authorization_id=authorization.id,
        state="live", owner_id="collector-pg", common_pts=100,
        lease_expires_at=as_beijing_aware(NOW + timedelta(seconds=seconds)).astimezone(zone),
    )
    session.add(state)
    session.flush()
    return state

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import AccountHealth
from app.models import AccountStatus, TgAccount, TgAccountOnlineState
from app.services._common import _now
from app.services.account_online_constants import ONLINE_LOW_FREQUENCY_PROBE_INTERVAL, ONLINE_PROBE_INTERVAL
from app.services.account_online_state import mark_stale_online_states, probe_due_online_states


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _account(session: Session, account_id: int = 101) -> TgAccount:
    account = TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"账号{account_id}",
        phone_masked=f"138****{account_id}",
        status=AccountStatus.ACTIVE.value,
        session_ciphertext="session",
        proxy_id=7,
    )
    session.add(account)
    session.commit()
    return account


def test_probe_due_online_states_keeps_stale_window_after_next_probe(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
            next_probe_at=now - timedelta(seconds=1),
        )
        session.add(state)
        session.commit()

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda _session, _account: object())
        monkeypatch.setattr(
            "app.services.account_online_probe.gateway.check_account_health",
            lambda _session_ciphertext, _credentials: AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用"),
        )

        assert probe_due_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert state.next_probe_at == now + ONLINE_PROBE_INTERVAL
        assert state.stale_after_at > state.next_probe_at


def test_mark_stale_online_states_requeues_probe_immediately():
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="online",
            stale_after_at=now - timedelta(seconds=1),
            next_probe_at=now + ONLINE_LOW_FREQUENCY_PROBE_INTERVAL,
        )
        session.add(state)
        session.commit()

        assert mark_stale_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert state.online_status == "offline"
        assert state.failure_type == "stale_probe"
        assert state.next_probe_at <= now

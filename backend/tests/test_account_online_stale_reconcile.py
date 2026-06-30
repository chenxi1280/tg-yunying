from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, Task, TgAccount, TgAccountOnlineState, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.account_online_state import mark_stale_online_states, reconcile_runtime_online_sources


pytestmark = pytest.mark.no_postgres


def test_runtime_reconcile_does_not_extend_stale_online_deadline():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now = _now()

    with Session(engine) as session:
        session.add(
            TgAccount(
                id=101,
                tenant_id=1,
                display_name="账号101",
                phone_masked="138****101",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="session",
            )
        )
        session.add(TgGroup(id=501, tenant_id=1, tg_peer_id="-100501", title="群501"))
        session.add(TgGroupAccount(id=501101, tenant_id=1, group_id=501, account_id=101, can_send=True))
        session.add(
            Task(
                id="ai-running",
                tenant_id=1,
                name="AI活群",
                type="group_ai_chat",
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [101]},
                type_config={"target_group_id": 501},
            )
        )
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=101,
                desired_online=True,
                desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
                online_status="online",
                stale_after_at=now - timedelta(seconds=1),
                last_seen_at=now - timedelta(minutes=5),
            )
        )
        session.commit()

        reconcile_runtime_online_sources(session, tenant_id=1, include_global=False, now=now)
        marked = mark_stale_online_states(session, now=now)
        session.commit()

        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        assert marked == 1
        assert state is not None
        assert state.online_status == "offline"
        assert state.failure_type == "stale_probe"

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import AccountHealth
from app.models import AccountStatus, Task, TgAccount, TgAccountOnlineState, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.account_online_state import (
    is_account_online_available,
    is_account_online_ready,
    is_account_online_ready_for_planning,
    probe_due_online_states,
    reconcile_account_online_sources,
    reconcile_runtime_online_sources,
)
from app.services.account_online_projection import task_account_online_summary


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


def _group(session: Session, group_id: int = 501) -> TgGroup:
    group = TgGroup(
        id=group_id,
        tenant_id=1,
        tg_peer_id=f"-100{group_id}",
        title=f"群{group_id}",
        listener_enabled=False,
    )
    session.add(group)
    session.commit()
    return group


def _link(session: Session, group_id: int, account_id: int, *, can_send: bool = True, is_listener: bool = False) -> None:
    session.add(
        TgGroupAccount(
            id=group_id * 1000 + account_id,
            tenant_id=1,
            group_id=group_id,
            account_id=account_id,
            can_send=can_send,
            is_listener=is_listener,
        )
    )
    session.commit()


def test_reconcile_online_sources_creates_traceable_desired_state():
    now = _now()
    with _session() as session:
        _account(session)

        changed = reconcile_account_online_sources(
            session,
            tenant_id=1,
            sources=[
                {
                    "source_type": "task",
                    "source_id": "task-1",
                    "account_ids": [101],
                    "session_kind": "primary",
                    "session_id": "auth-1",
                    "proxy_id": 7,
                }
            ],
            now=now,
        )
        session.commit()

        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        assert changed == 1
        assert state is not None
        assert state.desired_online is True
        assert state.desired_sources == [{"source_type": "task", "source_id": "task-1"}]
        assert state.active_task_count == 1
        assert state.session_kind == "primary"
        assert state.session_id == "auth-1"
        assert state.proxy_id == 7
        assert state.online_status == "warming"
        assert state.stale_after_at > now


def test_reconcile_clears_orphaned_desired_online_when_sources_disappear():
    now = _now()
    with _session() as session:
        _account(session)
        reconcile_account_online_sources(
            session,
            tenant_id=1,
            sources=[{"source_type": "task", "source_id": "task-1", "account_ids": [101]}],
            now=now,
        )
        session.commit()

        changed = reconcile_account_online_sources(session, tenant_id=1, sources=[], now=now + timedelta(minutes=1))
        session.commit()

        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        assert changed == 1
        assert state is not None
        assert state.desired_online is False
        assert state.desired_sources == []
        assert state.active_task_count == 0
        assert state.online_status == "offline"
        assert state.failure_type == "desired_source_removed"


def test_stale_online_state_is_not_ready_for_dispatch():
    now = _now()
    with _session() as session:
        _account(session)
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=101,
                desired_online=True,
                desired_sources=[{"source_type": "task", "source_id": "task-1"}],
                online_status="online",
                stale_after_at=now - timedelta(seconds=1),
                last_seen_at=now - timedelta(minutes=5),
            )
        )
        session.commit()

        assert is_account_online_ready(session, tenant_id=1, account_id=101, now=now) is False


def test_planning_ready_requires_traceable_online_state():
    now = _now()
    with _session() as session:
        _account(session)

        assert is_account_online_ready(session, tenant_id=1, account_id=101, now=now) is False
        assert is_account_online_ready_for_planning(session, tenant_id=1, account_id=101, now=now) is False

        session.add(TgAccountOnlineState(tenant_id=1, account_id=202, desired_online=True, online_status="online"))
        session.commit()

        assert is_account_online_ready_for_planning(session, tenant_id=1, account_id=101, now=now) is False

        session.add(TgAccountOnlineState(tenant_id=1, account_id=101, desired_online=True, online_status="warming"))
        session.commit()

        assert is_account_online_ready(session, tenant_id=1, account_id=101, now=now) is False
        assert is_account_online_available(session, tenant_id=1, account_id=101, now=now) is True
        assert is_account_online_ready_for_planning(session, tenant_id=1, account_id=101, now=now) is True


def test_probe_due_online_states_marks_healthy_account_online(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "global", "source_id": "global_keepalive"}],
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

        assert state.online_status == "online"
        assert state.failure_type == ""
        assert state.last_seen_at == now
        assert state.next_probe_at > now
        assert is_account_online_ready(session, tenant_id=1, account_id=101, now=now) is True


def test_probe_due_online_states_marks_session_failure_login_required(monkeypatch):
    now = _now()
    with _session() as session:
        account = _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
        )
        session.add(state)
        session.commit()

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda _session, _account: object())
        monkeypatch.setattr(
            "app.services.account_online_probe.gateway.check_account_health",
            lambda _session_ciphertext, _credentials: AccountHealth(status=AccountStatus.SESSION_EXPIRED.value, health_score=40, detail="session 已失效"),
        )

        assert probe_due_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert account.status == AccountStatus.SESSION_EXPIRED.value
        assert state.online_status == "login_required"
        assert state.failure_type == "account_unavailable"
        assert state.failure_detail == "session 已失效"
        assert state.next_probe_at > now


def test_probe_due_online_states_marks_missing_developer_app_blocked(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
        )
        session.add(state)
        session.commit()

        def _raise_missing_app(_session, _account):
            raise ValueError("账号未绑定可用 Telegram Developer App")

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", _raise_missing_app)

        assert probe_due_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert state.online_status == "blocked"
        assert state.failure_type == "developer_app_unavailable"
        assert "Developer App" in state.failure_detail


def test_runtime_reconcile_backfills_running_ai_relay_and_listener_sources():
    with _session() as session:
        _account(session, 101)
        _account(session, 102)
        _account(session, 103)
        ai_group = _group(session, 501)
        relay_source = _group(session, 601)
        relay_target = _group(session, 602)
        relay_source.listener_enabled = True
        _link(session, ai_group.id, 101, can_send=True)
        _link(session, ai_group.id, 102, can_send=False)
        _link(session, relay_source.id, 102, can_send=True, is_listener=True)
        _link(session, relay_target.id, 103, can_send=True)
        session.add_all(
            [
                Task(
                    id="ai-running",
                    tenant_id=1,
                    name="AI活群",
                    type="group_ai_chat",
                    status="running",
                    account_config={"selection_mode": "manual", "account_ids": [101, 102]},
                    type_config={"target_group_id": ai_group.id, "history_fetch_account_id": 102},
                ),
                Task(
                    id="relay-running",
                    tenant_id=1,
                    name="转发",
                    type="group_relay",
                    status="running",
                    account_config={"selection_mode": "all", "max_concurrent": 10},
                    type_config={
                        "source_groups": [{"group_id": relay_source.id, "is_active": True}],
                        "target_group_ids": [relay_target.id],
                    },
                ),
                Task(
                    id="ai-stopped",
                    tenant_id=1,
                    name="停用任务",
                    type="group_ai_chat",
                    status="stopped",
                    account_config={"selection_mode": "manual", "account_ids": [103]},
                    type_config={"target_group_id": relay_target.id},
                ),
            ]
        )
        session.commit()

        changed = reconcile_runtime_online_sources(session, tenant_id=1, include_global=False)
        session.commit()

        rows = {
            row.account_id: row
            for row in session.scalars(select(TgAccountOnlineState).where(TgAccountOnlineState.tenant_id == 1))
        }
        assert changed == 3
        assert rows[101].desired_sources == [{"source_type": "task", "source_id": "ai-running"}]
        assert _source_set(rows[102].desired_sources) == {
            ("task", "ai-running:history"),
            ("listener", "group:601"),
            ("task", "relay-running:source:601"),
        }
        assert rows[103].desired_sources == [{"source_type": "task", "source_id": "relay-running:target:602"}]
        assert rows[103].active_task_count == 1


def test_task_account_online_summary_counts_statuses_and_failure_samples():
    now = _now()
    with _session() as session:
        _account(session, 101)
        _account(session, 102)
        task = Task(
            id="ai-running",
            tenant_id=1,
            name="AI活群",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101, 102]},
        )
        session.add(task)
        session.add_all(
            [
                TgAccountOnlineState(
                    tenant_id=1,
                    account_id=101,
                    desired_online=True,
                    desired_sources=[{"source_type": "task", "source_id": task.id}],
                    online_status="online",
                    stale_after_at=now + timedelta(minutes=1),
                ),
                TgAccountOnlineState(
                    tenant_id=1,
                    account_id=102,
                    desired_online=True,
                    desired_sources=[{"source_type": "task", "source_id": task.id}],
                    online_status="online",
                    stale_after_at=now - timedelta(seconds=1),
                    failure_type="stale_probe",
                    failure_detail="在线状态超过 stale_after_at 未刷新",
                ),
            ]
        )
        session.commit()

        summary = task_account_online_summary(session, task, now=now)

        assert summary["desired_count"] == 2
        assert summary["online_count"] == 1
        assert summary["stale_count"] == 1
        assert summary["status_counts"]["online"] == 1
        assert summary["samples"][0]["account_id"] == 102
        assert summary["samples"][0]["failure_type"] == "stale_probe"


def test_task_account_online_summary_does_not_match_prefix_task_ids():
    now = _now()
    with _session() as session:
        _account(session, 101)
        task = Task(
            id="task-1",
            tenant_id=1,
            name="短 ID 任务",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": []},
        )
        session.add(task)
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=101,
                desired_online=True,
                desired_sources=[{"source_type": "task", "source_id": "task-10"}],
                online_status="online",
                stale_after_at=now + timedelta(minutes=5),
            )
        )
        session.commit()

        summary = task_account_online_summary(session, task, now=now)

        assert summary["desired_count"] == 0
        assert summary["online_count"] == 0


def test_runtime_reconcile_can_keep_all_active_session_accounts_online_globally():
    with _session() as session:
        _account(session, 101)
        _account(session, 102)
        inactive = _account(session, 103)
        inactive.status = AccountStatus.DISABLED.value
        no_session = _account(session, 104)
        no_session.session_ciphertext = ""
        session.commit()

        changed = reconcile_runtime_online_sources(session, tenant_id=1, include_global=True)
        session.commit()

        rows = {
            row.account_id: row
            for row in session.scalars(select(TgAccountOnlineState).where(TgAccountOnlineState.tenant_id == 1))
        }
        assert changed == 2
        assert set(rows) == {101, 102}
        assert rows[101].desired_sources == [{"source_type": "global", "source_id": "global_keepalive"}]


def _source_set(sources: list[dict[str, str]]) -> set[tuple[str, str]]:
    return {(source["source_type"], source["source_id"]) for source in sources}

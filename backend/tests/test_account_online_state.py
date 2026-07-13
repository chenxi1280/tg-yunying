from __future__ import annotations

import threading
from datetime import timedelta
from time import perf_counter

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.integrations.telegram import AccountHealth
from app.models import AccountPool, AccountStatus, Task, TaskAccountDailyCoverage, TgAccount, TgAccountOnlineState, TgGroup, TgGroupAccount
from app.services._common import _now
from app.services.account_online_state import (
    is_account_online_available,
    is_account_online_ready,
    is_account_online_ready_for_planning,
    probe_due_online_states,
    reconcile_account_online_sources,
    reconcile_runtime_online_sources,
)
from app.services.account_online_constants import ONLINE_PROBE_INTERVAL
from app.services.account_online_projection import task_account_online_summary
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _ensure_normal_pool(session: Session) -> AccountPool:
    pool = session.get(AccountPool, 1)
    if pool is None:
        pool = AccountPool(id=1, tenant_id=1, name="普通账号组", pool_purpose="normal", is_default=True)
        session.add(pool)
        session.flush()
    return pool


def _account(session: Session, account_id: int = 101) -> TgAccount:
    pool = _ensure_normal_pool(session)
    account = TgAccount(
        id=account_id,
        tenant_id=1,
        pool_id=pool.id,
        display_name=f"账号{account_id}",
        phone_masked=f"138****{account_id}",
        status=AccountStatus.ACTIVE.value,
        account_identity="normal",
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


def test_reconcile_online_sources_does_not_rewrite_unchanged_state():
    now = _now()
    sources = [{"source_type": "task", "source_id": "task-1", "account_ids": [101]}]
    with _session() as session:
        _account(session)
        reconcile_account_online_sources(session, tenant_id=1, sources=sources, now=now)
        session.commit()
        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        original_reconciled_at = state.reconciled_at
        original_updated_at = state.updated_at

        changed = reconcile_account_online_sources(
            session,
            tenant_id=1,
            sources=sources,
            now=now + timedelta(minutes=1),
        )

        assert changed == 0
        assert state.reconciled_at == original_reconciled_at
        assert state.updated_at == original_updated_at
        assert state not in session.dirty


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


def test_group_planning_checks_online_readiness_in_one_query():
    now = _now()
    with _session() as session:
        for account_id in range(101, 131):
            _account(session, account_id)
            session.add(
                TgAccountOnlineState(
                    tenant_id=1,
                    account_id=account_id,
                    desired_online=True,
                    online_status="online",
                    session_id=str(account_id),
                    proxy_id=7,
                    stale_after_at=now + timedelta(minutes=5),
                )
            )
        session.commit()
        accounts = list(session.scalars(select(TgAccount).where(TgAccount.id.between(101, 130))))
        task = type("TaskStub", (), {"tenant_id": 1, "stats": {}, "type_config": {}})()
        select_count = 0

        def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
            nonlocal select_count
            select_count += int(statement.lstrip().upper().startswith("SELECT"))

        event.listen(session.bind, "before_cursor_execute", count_selects)
        ready = group_ai_chat._online_ready_accounts(session, task, accounts, {})

        assert len(ready) == 30
        assert select_count <= 1


def _seed_online_scale(session: Session) -> None:
    accounts = [
        TgAccount(
            id=account_id,
            tenant_id=1,
            display_name=str(account_id),
            phone_masked=str(account_id),
            status=AccountStatus.ACTIVE.value,
            session_ciphertext=f"session-{account_id}",
            proxy_id=7,
        )
        for account_id in range(101, 681)
    ]
    groups = [
        TgGroup(id=group_id, tenant_id=1, tg_peer_id=f"-100{group_id}", title=str(group_id))
        for group_id in range(501, 505)
    ]
    tasks = [
        Task(
            id=f"online-scale-{group.id}", tenant_id=1, name=str(group.id), type="group_ai_chat",
            status="running", account_config={"selection_mode": "all"}, type_config={"target_group_id": group.id},
        )
        for group in groups
    ]
    links = [
        TgGroupAccount(tenant_id=1, group_id=group.id, account_id=account.id, can_send=True)
        for group in groups
        for account in accounts
    ]
    session.add_all([*accounts, *groups, *tasks, *links])
    session.commit()


def test_runtime_online_reconcile_580_accounts_four_tasks_is_bounded_and_write_free():
    now = _now()
    with _session() as session:
        _seed_online_scale(session)
        assert reconcile_runtime_online_sources(session, tenant_id=1, include_global=False, now=now) == 580
        session.commit()
        select_count = 0
        update_count = 0

        def count_statements(_conn, _cursor, statement, _parameters, _context, _executemany):
            nonlocal select_count, update_count
            normalized = statement.lstrip().upper()
            select_count += int(normalized.startswith("SELECT"))
            update_count += int(normalized.startswith("UPDATE"))

        event.listen(session.bind, "before_cursor_execute", count_statements)
        started_at = perf_counter()
        changed = reconcile_runtime_online_sources(session, tenant_id=1, include_global=False, now=now + timedelta(minutes=1))
        elapsed = perf_counter() - started_at
        session.flush()

        assert changed == 0
        assert select_count <= 8
        assert update_count == 0
        assert elapsed < 5


def test_online_ready_requires_recorded_proxy_to_match_current_account():
    now = _now()
    with _session() as session:
        account = _account(session)
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=101,
                desired_online=True,
                desired_sources=[{"source_type": "task", "source_id": "task-1"}],
                online_status="online",
                session_id="101",
                proxy_id=7,
                stale_after_at=now + timedelta(minutes=5),
                last_seen_at=now,
            )
        )
        session.commit()

        account.proxy_id = 8
        session.commit()

        assert is_account_online_ready(session, tenant_id=1, account_id=101, now=now) is False
        assert is_account_online_available(session, tenant_id=1, account_id=101, now=now) is False
        assert is_account_online_ready_for_planning(session, tenant_id=1, account_id=101, now=now) is False


def test_reconcile_clears_stale_proxy_when_account_becomes_direct():
    now = _now()
    with _session() as session:
        account = _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "task-1"}],
            online_status="online",
            session_id="101",
            proxy_id=7,
            stale_after_at=now + timedelta(minutes=5),
            last_seen_at=now,
        )
        session.add(state)
        session.commit()

        account.proxy_id = None
        changed = reconcile_account_online_sources(
            session,
            tenant_id=1,
            sources=[
                {
                    "source_type": "task",
                    "source_id": "task-1",
                    "account_ids": [101],
                    "session_id": "101",
                    "proxy_id": None,
                }
            ],
            now=now + timedelta(seconds=1),
        )
        session.commit()

        assert changed == 1
        assert state.proxy_id is None
        assert is_account_online_ready_for_planning(session, tenant_id=1, account_id=101, now=now) is True


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
        assert is_account_online_ready_for_planning(session, tenant_id=1, account_id=101, now=now) is False


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

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
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


def test_online_probe_releases_daily_offline_blocker_for_sendable_group(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        _group(session)
        _link(session, 501, 101, can_send=True)
        session.add(Task(id="coverage-online", tenant_id=1, name="在线恢复", type="group_ai_chat", status="running"))
        coverage = TaskAccountDailyCoverage(
            id="coverage-online-row",
            tenant_id=1,
            task_id="coverage-online",
            group_id=501,
            account_id=101,
            coverage_date=now.date(),
            state="blocked",
            blocker_code="account_offline",
            blocker_detail="账号实时在线状态不可用",
        )
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "coverage-online"}],
            online_status="offline",
            next_probe_at=now - timedelta(seconds=1),
        )
        session.add_all([coverage, state])
        session.commit()
        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            "app.services.account_online_probe.gateway.check_account_health",
            lambda *_args: AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用"),
        )

        assert probe_due_online_states(session, limit=10, now=now) == 1
        session.refresh(coverage)

        assert coverage.state == "ready"
        assert coverage.blocker_code == ""


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

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
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


def test_probe_due_online_states_continues_after_auth_key_duplicate(monkeypatch):
    class AuthKeyDuplicatedError(Exception):
        pass

    now = _now()
    with _session() as session:
        first = _account(session, 101)
        second = _account(session, 102)
        first_state = TgAccountOnlineState(
            tenant_id=1,
            account_id=first.id,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
        )
        second_state = TgAccountOnlineState(
            tenant_id=1,
            account_id=second.id,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="warming",
        )
        session.add_all([first_state, second_state])
        session.commit()

        def _check_health(session_ciphertext, _credentials):
            if session_ciphertext == "session-duplicated":
                raise AuthKeyDuplicatedError("authorization key duplicated")
            return AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用")

        first.session_ciphertext = "session-duplicated"
        second.session_ciphertext = "session-ok"
        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr("app.services.account_online_probe.gateway.check_account_health", _check_health)

        assert probe_due_online_states(session, limit=10, now=now) == 2
        session.commit()

        assert first.status == AccountStatus.SESSION_EXPIRED.value
        assert first_state.online_status == "login_required"
        assert first_state.failure_type == "account_unavailable"
        assert "AuthKeyDuplicatedError" in first_state.failure_detail
        assert first_state.next_probe_at > now
        assert second.status == AccountStatus.ACTIVE.value
        assert second_state.online_status == "online"
        assert second_state.failure_type == ""


def test_probe_due_online_states_runs_health_checks_concurrently(monkeypatch):
    now = _now()
    active = 0
    max_active = 0
    lock = threading.Lock()
    release = threading.Event()

    def check_health(_session_ciphertext, _credentials):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active >= 2:
                release.set()
        release.wait(timeout=0.2)
        with lock:
            active -= 1
        return AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用")

    with _session() as session:
        for account_id in range(101, 105):
            _account(session, account_id)
            session.add(TgAccountOnlineState(
                tenant_id=1,
                account_id=account_id,
                desired_online=True,
                desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
                online_status="warming",
                next_probe_at=now - timedelta(seconds=1),
            ))
        session.commit()
        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr("app.services.account_online_probe.gateway.check_account_health", check_health)

        assert probe_due_online_states(session, limit=10, now=now) == 4

    assert max_active >= 2


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

        def _raise_missing_app(*_args, **_kwargs):
            raise ValueError("账号未绑定可用 Telegram Developer App")

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", _raise_missing_app)

        assert probe_due_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert state.online_status == "blocked"
        assert state.failure_type == "developer_app_unavailable"
        assert "Developer App" in state.failure_detail


def test_probe_due_online_states_retries_due_blocked_state(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-running"}],
            online_status="blocked",
            failure_type="account_health_probe_failed",
            failure_detail="TimeoutError: ",
            next_probe_at=now - timedelta(seconds=1),
        )
        session.add(state)
        session.commit()

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
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


def test_runtime_reconcile_retains_paused_task_as_low_frequency_keepalive_source():
    now = _now()
    with _session() as session:
        _account(session, 101)
        group = _group(session, 501)
        _link(session, group.id, 101, can_send=True)
        session.add(
            Task(
                id="ai-paused",
                tenant_id=1,
                name="暂停 AI 活群",
                type="group_ai_chat",
                status="paused",
                account_config={"selection_mode": "manual", "account_ids": [101]},
                type_config={"target_group_id": group.id},
            )
        )
        session.commit()

        changed = reconcile_runtime_online_sources(session, tenant_id=1, include_global=False, now=now)
        session.commit()

        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        assert changed == 1
        assert state is not None
        assert state.desired_online is True
        assert state.desired_sources == [
            {"source_type": "task", "source_id": "ai-paused", "keepalive_mode": "low_frequency"}
        ]
        assert state.active_task_count == 0
        assert state.stale_after_at == now + timedelta(minutes=25)


def test_low_frequency_only_to_active_requires_fresh_probe(monkeypatch):
    paused_at = _now()
    resumed_at = paused_at + timedelta(minutes=1)
    sources = [{"source_type": "task", "source_id": "ai", "account_ids": [101], "keepalive_mode": "low_frequency"}]
    with _session() as session:
        _account(session, 101)
        reconcile_account_online_sources(session, tenant_id=1, sources=sources, now=paused_at)
        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        state.online_status = "online"
        state.next_probe_at = paused_at + timedelta(minutes=15)
        state.stale_after_at = paused_at + timedelta(minutes=25)
        session.commit()

        sources[0].pop("keepalive_mode")
        assert reconcile_account_online_sources(session, tenant_id=1, sources=sources, now=resumed_at) == 1
        assert state.online_status == "warming"
        assert state.next_probe_at == resumed_at
        assert state.stale_after_at == resumed_at + timedelta(minutes=15)
        assert is_account_online_ready_for_planning(session, tenant_id=1, account_id=101, now=resumed_at) is False
        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            "app.services.account_online_probe.gateway.check_account_health",
            lambda *_args: AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="ok"),
        )
        assert probe_due_online_states(session, limit=10, now=resumed_at) == 1
        assert state.online_status == "online"
        assert state.next_probe_at == resumed_at + timedelta(minutes=5)
        assert state.stale_after_at == resumed_at + timedelta(minutes=15)


def test_existing_active_source_is_not_blocked_when_paused_task_resumes():
    now = _now()
    old_sources = [
        {"source_type": "global", "source_id": "global_keepalive", "account_ids": [101]},
        {"source_type": "task", "source_id": "ai", "account_ids": [101], "keepalive_mode": "low_frequency"},
    ]
    with _session() as session:
        _account(session, 101)
        reconcile_account_online_sources(session, tenant_id=1, sources=old_sources, now=now)
        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        state.online_status = "online"
        state.next_probe_at = now + timedelta(minutes=5)
        state.stale_after_at = now + timedelta(minutes=15)
        session.commit()

        active_sources = [dict(source) for source in old_sources]
        active_sources[1].pop("keepalive_mode")
        reconcile_account_online_sources(session, tenant_id=1, sources=active_sources, now=now + timedelta(minutes=1))

        assert state.online_status == "online"
        assert state.next_probe_at == now + timedelta(minutes=5)
        assert state.stale_after_at == now + timedelta(minutes=15)


def test_probe_due_online_states_uses_longer_interval_for_low_frequency_sources(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        state = TgAccountOnlineState(
            tenant_id=1,
            account_id=101,
            desired_online=True,
            desired_sources=[{"source_type": "task", "source_id": "ai-paused", "keepalive_mode": "low_frequency"}],
            online_status="warming",
            next_probe_at=now - timedelta(seconds=1),
        )
        session.add(state)
        session.commit()

        monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_args, **_kwargs: object())
        monkeypatch.setattr(
            "app.services.account_online_probe.gateway.check_account_health",
            lambda _session_ciphertext, _credentials: AccountHealth(status=AccountStatus.ACTIVE.value, health_score=96, detail="账号 session 可用"),
        )

        assert probe_due_online_states(session, limit=10, now=now) == 1
        session.commit()

        assert state.online_status == "online"
        assert state.next_probe_at > now + ONLINE_PROBE_INTERVAL
        assert state.stale_after_at > state.next_probe_at

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


def test_global_keepalive_keeps_consistent_dedicated_accounts_for_health_probe():
    with _session() as session:
        session.add(AccountPool(id=2, tenant_id=1, name="接码账号组", pool_purpose="code_receiver"))
        session.add(AccountPool(id=3, tenant_id=1, name="降权账号组", pool_purpose="rank_deboost"))
        session.add(
            TgAccount(
                id=201,
                tenant_id=1,
                pool_id=2,
                display_name="接码账号",
                phone_masked="201",
                status=AccountStatus.ACTIVE.value,
                account_identity="code_receiver",
                session_ciphertext="session-201",
            )
        )
        session.add(
            TgAccount(
                id=202,
                tenant_id=1,
                pool_id=3,
                display_name="降权账号",
                phone_masked="202",
                status=AccountStatus.ACTIVE.value,
                account_identity="rank_deboost",
                session_ciphertext="session-202",
            )
        )
        session.commit()

        changed = reconcile_runtime_online_sources(session, tenant_id=1, include_global=True)
        session.commit()

        rows = {
            row.account_id: row
            for row in session.scalars(select(TgAccountOnlineState).where(TgAccountOnlineState.tenant_id == 1))
        }
        assert changed == 2
        assert set(rows) == {201, 202}
        assert rows[201].desired_sources == [{"source_type": "global", "source_id": "global_keepalive"}]
        assert rows[202].desired_sources == [{"source_type": "global", "source_id": "global_keepalive"}]


def test_global_keepalive_retains_login_required_account_for_recovery():
    now = _now()
    with _session() as session:
        account = _account(session, 101)
        account.status = AccountStatus.SESSION_EXPIRED.value
        session.add(
            TgAccountOnlineState(
                tenant_id=1,
                account_id=101,
                desired_online=True,
                desired_sources=[{"source_type": "global", "source_id": "global_keepalive"}],
                online_status="login_required",
                failure_type="account_unavailable",
                failure_detail="session 已失效",
                reconciled_at=now - timedelta(minutes=10),
            )
        )
        session.commit()

        changed = reconcile_runtime_online_sources(session, tenant_id=1, include_global=True, now=now)
        session.commit()

        state = session.scalar(select(TgAccountOnlineState).where(TgAccountOnlineState.account_id == 101))
        assert changed == 1
        assert state is not None
        assert state.desired_online is True
        assert state.desired_sources == [{"source_type": "global", "source_id": "global_keepalive"}]
        assert state.online_status == "login_required"


def _source_set(sources: list[dict[str, str]]) -> set[tuple[str, str]]:
    return {(source["source_type"], source["source_id"]) for source in sources}

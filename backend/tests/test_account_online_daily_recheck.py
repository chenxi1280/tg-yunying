from datetime import timedelta

import pytest

from app.integrations.telegram import AccountHealth
from app.models import AccountStatus, TgAccountOnlineState
from app.services._common import _now
from app.services.account_online_constants import ONLINE_UNAVAILABLE_PROBE_INTERVAL
from app.services.account_online_probe import OnlineProbeResult, probe_due_online_states
from app.services.account_online_reconciliation import apply_desired_state
from app.services.account_online_state import mark_stale_online_states
from test_account_online_probe_timing import _account, _session


pytestmark = pytest.mark.no_postgres
DAY = timedelta(days=1)


def _state(session, now, **values):
    state = TgAccountOnlineState(
        tenant_id=1, account_id=101, desired_online=True,
        online_status="blocked", last_probe_at=now,
        next_probe_at=now + timedelta(minutes=5), **values,
    )
    session.add(state)
    session.commit()
    return state


def _probe(monkeypatch, result):
    calls = []
    monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", lambda *_: object())

    def run(jobs):
        calls.extend(jobs)
        return iter([result] if jobs else [])

    monkeypatch.setattr("app.services.account_online_probe._run_health_probes", run)
    return calls


@pytest.mark.parametrize("status,frozen", [
    (AccountStatus.SESSION_EXPIRED.value, False),
    (AccountStatus.NEED_RELOGIN.value, False),
    (AccountStatus.SUSPECTED_BANNED.value, True),
    (AccountStatus.ACTIVE.value, True),
])
@pytest.mark.parametrize("failure", ["network", "credentials", "unavailable"])
def test_known_failure_obeys_day_despite_old_deadline_and_retry_failure(monkeypatch, status, frozen, failure):
    now = _now()
    with _session() as session:
        account = _account(session)
        account.status, account.telegram_frozen = status, frozen
        state = _state(session, now)
        result = OnlineProbeResult(
            account_id=101,
            error=TimeoutError("network") if failure == "network" else None,
            health=None if failure == "network" else AccountHealth(status=status, health_score=0, detail="unavailable"),
        )
        calls = _probe(monkeypatch, result)
        if failure == "credentials":
            def no_credentials(*_):
                raise ValueError("developer app unavailable")
            monkeypatch.setattr("app.services.account_online_probe.credentials_for_account", no_credentials)
        assert probe_due_online_states(session, now=now + timedelta(minutes=30)) == 0
        assert probe_due_online_states(session, now=now + DAY - timedelta(microseconds=1)) == 0
        assert calls == []
        assert probe_due_online_states(session, now=now + DAY) == 1
        session.commit()
        assert state.next_probe_at == now + DAY * 2
        assert len(calls) == (0 if failure == "credentials" else 1)
        assert probe_due_online_states(session, now=now + DAY + timedelta(hours=1)) == 0


@pytest.mark.parametrize("status", [AccountStatus.BANNED.value, AccountStatus.DISABLED.value])
def test_banned_disabled_residual_online_demand_never_calls_telegram(monkeypatch, status):
    now = _now()
    with _session() as session:
        account = _account(session)
        account.status = status
        _state(session, now - DAY * 2)
        calls = _probe(monkeypatch, OnlineProbeResult(account_id=101))
        assert probe_due_online_states(session, now=now) == 0
        assert calls == []
        assert account.status == status


def test_new_login_required_result_waits_a_day(monkeypatch):
    now = _now()
    with _session() as session:
        _account(session)
        state = _state(session, now - timedelta(hours=1))
        calls = _probe(monkeypatch, OnlineProbeResult(account_id=101, health=AccountHealth(
            status=AccountStatus.NEED_RELOGIN.value, health_score=0, detail="login required")))
        assert probe_due_online_states(session, now=now) == 1
        session.commit()
        assert state.online_status == "login_required"
        assert state.next_probe_at == now + DAY
        assert probe_due_online_states(session, now=now + timedelta(minutes=30)) == 0
        assert len(calls) == 1


def test_frozen_batch_uses_completion_time_and_reconciliation_cannot_shorten_day(monkeypatch):
    now = _now()
    completed = now + timedelta(minutes=2)
    with _session() as session:
        account = _account(session)
        account.telegram_frozen = True
        state = _state(session, now - DAY)
        state.last_probe_at = None
        session.commit()
        _probe(monkeypatch, OnlineProbeResult(account_id=101, error=TimeoutError("network"), completed_at=completed))
        monkeypatch.setattr("app.services.account_online_probe._now", lambda: completed)
        assert probe_due_online_states(session, commit_each=True) == 1
        assert state.next_probe_at == completed + DAY
        apply_desired_state(state, {"sources": [{"source_type": "task", "source_id": "new"}]}, completed)
        assert state.next_probe_at == completed + DAY
        assert probe_due_online_states(session, now=completed + timedelta(hours=1)) == 0


def test_full_fresh_unfreeze_returns_to_normal_interval(monkeypatch):
    now = _now()
    with _session() as session:
        account = _account(session)
        account.telegram_frozen = True
        account.status = AccountStatus.SUSPECTED_BANNED.value
        state = _state(session, now - DAY)
        _probe(monkeypatch, OnlineProbeResult(account_id=101, health=AccountHealth(
            status=AccountStatus.ACTIVE.value, health_score=96, detail="verified",
            telegram_frozen=False, freeze_observed_at=now)))
        assert probe_due_online_states(session, now=now) == 1
        session.commit()
        assert not account.telegram_frozen
        assert account.status == AccountStatus.ACTIVE.value
        assert state.next_probe_at == now + timedelta(minutes=5)


def test_daily_candidates_do_not_starve_healthy_account_at_limit(monkeypatch):
    now = _now()
    with _session() as session:
        account = _account(session)
        account.status = AccountStatus.SESSION_EXPIRED.value
        _state(session, now - timedelta(hours=1))
        _account(session, 102)
        session.add(TgAccountOnlineState(tenant_id=1, account_id=102, desired_online=True,
            online_status="online", next_probe_at=now, last_probe_at=now - timedelta(minutes=5)))
        session.commit()
        calls = _probe(monkeypatch, OnlineProbeResult(account_id=102, error=TimeoutError("network")))
        assert probe_due_online_states(session, limit=1, now=now) == 1
        assert [job.account_id for job in calls] == [102]
        state = session.query(TgAccountOnlineState).filter_by(account_id=102).one()
        assert state.next_probe_at == now + timedelta(minutes=5)


def test_stale_online_projection_cannot_force_an_early_frozen_probe(monkeypatch):
    now = _now()
    with _session() as session:
        account = _account(session)
        account.telegram_frozen = True
        state = _state(session, now - timedelta(hours=1))
        state.online_status = "online"
        state.stale_after_at = now - timedelta(minutes=1)
        session.commit()
        calls = _probe(monkeypatch, OnlineProbeResult(account_id=101))
        assert mark_stale_online_states(session, now=now) == 1
        assert probe_due_online_states(session, now=now) == 0
        assert calls == []
        assert ONLINE_UNAVAILABLE_PROBE_INTERVAL == DAY


def test_authorization_switch_can_verify_immediately_before_daily_deadline(monkeypatch):
    from app.services.account_authorizations import _reset_online_state_after_authorization_switch

    now = _now()
    with _session() as session:
        account = _account(session)
        account.status = AccountStatus.SESSION_EXPIRED.value
        state = _state(session, now)
        state.next_probe_at = now + DAY
        session.commit()
        _probe(monkeypatch, OnlineProbeResult(account_id=101, health=AccountHealth(
            status=AccountStatus.ACTIVE.value, health_score=95, detail="new authorization verified")))
        assert probe_due_online_states(session, now=now + timedelta(minutes=1)) == 0
        _reset_online_state_after_authorization_switch(session, account)
        session.commit()
        assert probe_due_online_states(session, now=now + timedelta(minutes=1)) == 1
        assert account.status == AccountStatus.ACTIVE.value
